"""最小托盘客户端：只做 UI，状态与配置一律经由 IPC 与服务交互。"""
from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import pystray
import win32api
import win32event
from PIL import Image, ImageDraw

from ..config import CARRIER_SUFFIX
from ..ipc import IpcError, PipeClient
from . import autostart

POLL_SECONDS = 5
SINGLE_INSTANCE_NAME = "Local\\NetAnchorCampus.Tray"
ERROR_ALREADY_EXISTS = 183

_instance_mutex = None


def acquire_single_instance(name: str = SINGLE_INSTANCE_NAME) -> bool:
    """拿命名互斥体；已有实例时返回 False。"""
    global _instance_mutex
    handle = win32event.CreateMutex(None, False, name)
    if win32api.GetLastError() == ERROR_ALREADY_EXISTS:
        win32api.CloseHandle(handle)
        return False
    _instance_mutex = handle
    return True


def release_single_instance() -> None:
    global _instance_mutex
    if _instance_mutex is not None:
        win32api.CloseHandle(_instance_mutex)
        _instance_mutex = None

COLORS = {
    "online": "#2ecc71",
    "reconnecting": "#e74c3c",
    "portal_unreachable": "#e67e22",
    "config_missing": "#e67e22",
    "stopped": "#95a5a6",
}

STATE_LABELS = {
    "online": "已连接",
    "reconnecting": "重连中",
    "portal_unreachable": "门户不可达",
    "config_missing": "缺少配置",
    "stopped": "已停止",
}


def color_for_state(state: str) -> str:
    return COLORS.get(state, "#95a5a6")


def status_text(status: dict) -> str:
    state = str(status.get("state", "stopped"))
    label = STATE_LABELS.get(state, state)
    detail = status.get("last_error") or ""
    if state == "online":
        return f"状态：{label}"
    if detail:
        return f"状态：{label}（{detail}）"
    return f"状态：{label}"


def make_icon(color: str, size: int = 64) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = 4
    draw.ellipse([margin, margin, size - margin, size - margin], fill=color, outline="white", width=2)
    return image


class TrayApp:
    def __init__(self, client: PipeClient | None = None) -> None:
        self._client = client or PipeClient()
        self._status: dict = {}
        self._error = ""
        self._icon = pystray.Icon("netanchor", make_icon("#95a5a6"), "NetAnchor-Campus")
        self._icon.menu = self._build_menu()
        self._stop = threading.Event()

    # ---- IPC ----
    def _call(self, cmd: str, data: dict | None = None) -> dict:
        response = self._client.request(cmd, data)
        if not response.get("ok"):
            error = response.get("error") or {}
            raise IpcError(f"{error.get('code', 'error')}: {error.get('message', '')}")
        return response.get("data") or {}

    def _safe_call(self, cmd: str, data: dict | None = None) -> dict | None:
        try:
            self._error = ""
            return self._call(cmd, data)
        except IpcError as exc:
            self._error = str(exc)
            return None

    # ---- 刷新 ----
    def refresh(self) -> None:
        data = self._safe_call("status")
        if data is not None:
            self._status = data
        state = str(self._status.get("state", "stopped")) if data is not None else "stopped"
        title = status_text(self._status) if data is not None else "服务未连接"
        self._icon.icon = make_icon(color_for_state(state) if data is not None else "#95a5a6")
        self._icon.title = f"NetAnchor-Campus - {title}"
        self._icon.menu = self._build_menu()
        try:
            self._icon.update_menu()
        except Exception:
            pass

    def _build_menu(self):
        connected = bool(self._status) and not self._error
        status_label = status_text(self._status) if connected else "服务未连接"
        return pystray.Menu(
            pystray.MenuItem(status_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即重连", self._on_reconnect, enabled=connected),
            pystray.MenuItem("启动监控", self._on_start, enabled=connected),
            pystray.MenuItem("停止监控", self._on_stop, enabled=connected),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("设置…", self._on_settings),
            pystray.MenuItem("打开日志目录", self._on_logs),
            pystray.MenuItem(
                f"开机自启托盘: {'[已启用]' if autostart.is_enabled() else '[未启用]'}",
                self._on_toggle_autostart,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出托盘", self._on_quit),
        )

    # ---- 菜单动作 ----
    def _on_toggle_autostart(self, icon=None, item=None) -> None:
        autostart.apply_setting(enabled=not autostart.setting_enabled())
        self._icon.menu = self._build_menu()
        try:
            self._icon.update_menu()
        except Exception:
            pass
    def _on_reconnect(self, icon=None, item=None) -> None:
        self._safe_call("reconnect")

    def _on_start(self, icon=None, item=None) -> None:
        self._safe_call("monitor.start")

    def _on_stop(self, icon=None, item=None) -> None:
        self._safe_call("monitor.stop")

    def _on_logs(self, icon=None, item=None) -> None:
        data = self._safe_call("logs.path")
        if not data:
            messagebox.showerror("NetAnchor-Campus", self._error or "无法获取日志目录")
            return
        os.startfile(str(data["path"]))

    def _on_quit(self, icon=None, item=None) -> None:
        self._stop.set()
        self._icon.stop()

    def _on_settings(self, icon=None, item=None) -> None:
        threading.Thread(target=self._settings_window, daemon=True).start()

    # ---- 设置窗口 ----
    def _settings_window(self) -> None:
        try:
            current = self._call("config.get")["config"]
        except IpcError as exc:
            messagebox.showerror("NetAnchor-Campus", f"读取配置失败：{exc}")
            return

        account = current.get("account", {})
        candidate = (current.get("portal", {}).get("candidates") or [{}])[0]
        monitor = current.get("monitor", {})

        root = tk.Tk()
        root.title("NetAnchor-Campus 设置")
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)

        entries = {}

        def row(label: str, value, show=None):
            holder = ttk.Frame(frame)
            holder.pack(fill="x", pady=3)
            ttk.Label(holder, text=label, width=12).pack(side="left")
            var = tk.StringVar(value=str(value))
            ttk.Entry(holder, textvariable=var, width=32, show=show).pack(side="left")
            return var

        ttk.Label(frame, text="账号", font=("", 10, "bold")).pack(anchor="w")
        entries["username"] = row("学号", account.get("username", ""))
        entries["password"] = row("新密码", "", show="*")

        carrier_row = ttk.Frame(frame)
        carrier_row.pack(fill="x", pady=3)
        ttk.Label(carrier_row, text="运营商", width=12).pack(side="left")
        carrier_var = tk.StringVar(value=account.get("carrier", "校园其他"))
        ttk.Combobox(carrier_row, textvariable=carrier_var, values=list(CARRIER_SUFFIX), state="readonly", width=29).pack(side="left")
        entries["carrier"] = carrier_var

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(frame, text="门户", font=("", 10, "bold")).pack(anchor="w")
        entries["host"] = row("门户地址", candidate.get("host", ""))
        entries["login_port"] = row("登录端口", candidate.get("login_port", 80))
        entries["eportal_port"] = row("取码端口", candidate.get("eportal_port", 801))

        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=10)
        entries["interval"] = row("检测间隔(秒)", monitor.get("check_interval_seconds", 30))

        ttk.Label(
            frame,
            text="密码以本机加密存储。修改门户地址时，需要同时重新输入密码。",
            foreground="gray",
            wraplength=380,
        ).pack(anchor="w", pady=(8, 0))

        def save() -> None:
            try:
                config = current
                config["portal"]["candidates"][0].update(
                    {
                        "host": entries["host"].get().strip(),
                        "login_port": int(entries["login_port"].get()),
                        "eportal_port": int(entries["eportal_port"].get()),
                    }
                )
                config["monitor"]["check_interval_seconds"] = int(entries["interval"].get())
                config["account"]["carrier"] = entries["carrier"].get()
                config["account"]["username"] = entries["username"].get().strip()

                payload = {"config": config}
                password = entries["password"].get()
                if password:
                    payload["password"] = password
                result = self._call("config.set", payload)

                message = "配置已保存"
                if result.get("password_rebound"):
                    message += "，密码已更新"
                messagebox.showinfo("NetAnchor-Campus", message)
                root.destroy()
            except IpcError as exc:
                text = str(exc)
                if "reauth_required" in text:
                    messagebox.showwarning(
                        "NetAnchor-Campus",
                        "门户地址已变更，请在“新密码”里重新输入一次密码后再保存。",
                    )
                else:
                    messagebox.showerror("NetAnchor-Campus", f"保存失败：{text}")
            except ValueError as exc:
                messagebox.showerror("NetAnchor-Campus", f"输入无效：{exc}")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="保存", command=save).pack(side="right")
        ttk.Button(buttons, text="取消", command=root.destroy).pack(side="right", padx=(0, 8))

        root.mainloop()

    # ---- 主循环 ----
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            time.sleep(POLL_SECONDS)

    def run(self) -> None:
        threading.Thread(target=self._poll_loop, daemon=True, name="TrayPoll").start()
        self._icon.run()


def main() -> int:
    if not acquire_single_instance():
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, "NetAnchor-Campus 托盘已经在运行。", "NetAnchor-Campus", 0x40
            )
        except Exception:  # noqa: BLE001
            pass
        return 0
    try:
        # 用户若开着"开机自启"，每次启动都确保注册项指向当前 exe（换目录后也能自愈）
        autostart.apply_setting()
        TrayApp().run()
    finally:
        release_single_instance()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
