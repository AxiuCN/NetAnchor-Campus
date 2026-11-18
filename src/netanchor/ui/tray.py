"""最小托盘客户端：只做 UI，状态与配置一律经由 IPC 与服务交互。"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import pystray
import win32api
import win32con
import win32event
import win32gui
from PIL import Image, ImageDraw

from .. import __version__
from ..config import CARRIER_SUFFIX
from ..ipc import IpcError, PipeClient
from ..logging_setup import setup_logging
from . import autostart

logger = logging.getLogger(__name__)

POLL_SECONDS = 5
SINGLE_INSTANCE_NAME = "Local\\NetAnchorCampus.Tray"
ERROR_ALREADY_EXISTS = 183
LOG_DIR_NAME = "NetAnchor-Campus"
# 自己建一个顶层窗口专门接收关机/注销消息（pystray 的窗口过程不处理这两条）
SESSION_WINDOW_CLASS = "NetAnchorSessionEndWatcher"
# 安装器（NetAnchor.Cli prepare）请托盘退出的注册消息，跨进程同名同值
TRAY_QUIT_MESSAGE = "NetAnchor-Tray-Quit"

_instance_mutex = None


def enable_dpi_awareness() -> None:
    """声明进程 DPI 感知。

    不做这一步时，125%/150% 缩放下 Windows 会把窗口按位图拉伸，字发虚、
    控件边缘也糊。必须在创建任何窗口之前调用。
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
        return
    except Exception:  # noqa: BLE001 - 老系统没有 shcore，或已经设置过
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


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


ASSET_NAME = "app.png"
_asset_cache: dict[str, Image.Image] = {}


def asset_path(name: str = ASSET_NAME) -> Path:
    """素材路径：打包后从 `_internal` 取，开发态从源码旁的 assets/ 取。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "netanchor" / "ui" / "assets" / name
    return Path(__file__).resolve().parent / "assets" / name


def _base_icon() -> Image.Image | None:
    """载入素材并裁到内容边界。

    母版为 exe 图标留了 6% 边距，但托盘里图标只有 16–20px，这点边距会让它
    明显比旁边满幅的应用小一圈，所以托盘单独裁紧。
    """
    if "base" not in _asset_cache:
        try:
            image = Image.open(asset_path()).convert("RGBA")
            bbox = image.getchannel("A").getbbox()
            _asset_cache["base"] = image.crop(bbox) if bbox else image
        except OSError as exc:
            logger.warning("加载图标素材失败，退回纯色图标：%s", exc)
            _asset_cache["base"] = None
    return _asset_cache["base"]


def make_icon(color: str, size: int = 256) -> Image.Image:
    """应用图标（铺满）+ 右上角状态角标。素材缺失时退回纯色圆点，托盘不能因为图标挂掉。"""
    base = _base_icon()
    if base is None:
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        margin = max(2, size // 16)
        draw.ellipse(
            [margin, margin, size - margin, size - margin],
            fill=color, outline="white", width=max(1, size // 32),
        )
        return image

    # 保持比例铺满：内容 bbox 不是正方形，直接 resize 会压扁
    scale = min(size / base.width, size / base.height)
    art_size = (max(1, round(base.width * scale)), max(1, round(base.height * scale)))
    art = base.resize(art_size, Image.LANCZOS)
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.paste(art, ((size - art_size[0]) // 2, (size - art_size[1]) // 2), art)

    # 角标放右下角：那里是浅色脸部，20px 下也有足够对比；放右上角会被深色帽檐吃掉
    radius = max(2, int(size * 0.20))
    gap = max(1, size // 64)
    cx = size - radius - gap
    cy = size - radius - gap
    ImageDraw.Draw(icon).ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        fill=color, outline=(255, 255, 255, 255), width=max(1, size // 48),
    )
    return icon


class TrayApp:
    def __init__(self, client: PipeClient | None = None) -> None:
        self._client = client or PipeClient()
        self._status: dict = {}
        self._error = ""
        self._connected = False
        self._logged_connected = False
        self._color = "#95a5a6"
        self._icons: dict[str, Image.Image] = {}
        self._stop = threading.Event()
        self._icon = pystray.Icon("netanchor", self._icon_for("#95a5a6"), "NetAnchor-Campus")
        self._icon.menu = self._build_menu()
        self._menu_signature = self._menu_state()

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
            # 轮询每 5 秒一次，失败原因由 refresh 在状态切换时汇总记录，这里只留明细
            logger.debug("IPC 调用 %s 失败: %s", cmd, exc)
            return None

    # ---- 刷新 ----
    def _icon_for(self, color: str) -> Image.Image:
        if color not in self._icons:
            self._icons[color] = make_icon(color)
        return self._icons[color]

    def _menu_state(self) -> tuple[bool, str, str, bool]:
        """菜单里会变化的内容。只有它变了才重建菜单。"""
        state = str(self._status.get("state", "stopped")) if self._connected else "stopped"
        detail = str(self._status.get("last_error") or "") if self._connected else ""
        return self._connected, state, detail, self._autostart_enabled()

    def refresh(self) -> None:
        data = self._safe_call("status")
        if data is not None:
            self._status = data
        self._connected = data is not None

        state = str(self._status.get("state", "stopped")) if self._connected else "stopped"
        title = status_text(self._status) if self._connected else "服务未连接"
        color = color_for_state(state) if self._connected else "#95a5a6"
        # pystray 的 visible setter 是「先 _update_icon 再 _show，最后才置 _visible」：
        # 图标还没挂进托盘时赋值会被静默丢弃，而 _icon_valid 已置 True，此后不再重试，
        # 角标就永久停在初始灰。所以只在图标确实可见时才落色，不可见留给下一轮。
        if color != self._color and self._icon.visible:
            self._color = color
            self._icon.icon = self._icon_for(color)
            # 图标是否真的换掉，从日志就能判断，不必靠肉眼盯托盘
            logger.info("托盘图标状态色 -> %s（%s）", color, state)
        self._icon.title = f"NetAnchor-Campus - {title}"

        # 赋值 icon.menu 会触发 pystray 的内部 update_menu（销毁并重建原生菜单句柄）。
        # 只在内容真的变了才重建：句柄重建和消息循环里的 TrackPopupMenu 抢同一个对象，
        # 每 5 秒无脑重建是自找竞态。
        signature = self._menu_state()
        if signature != self._menu_signature:
            self._menu_signature = signature
            self._icon.menu = self._build_menu()
            logger.info("托盘状态更新：%s", title)

        if self._connected != self._logged_connected:
            self._logged_connected = self._connected
            if self._connected:
                logger.info("已连上服务：%s", title)
            else:
                logger.warning("与服务失联：%s", self._error or "未知原因")

    def _build_menu(self):
        connected = self._connected and not self._error
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
                f"开机自启托盘: {'[已启用]' if self._autostart_enabled() else '[未启用]'}",
                self._on_toggle_autostart,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出托盘", self._on_quit),
        )

    @staticmethod
    def _autostart_enabled() -> bool:
        try:
            return autostart.is_enabled()
        except OSError:
            return False

    # ---- 菜单动作 ----
    def _action(self, cmd: str, label: str) -> None:
        # 菜单回调跑在 pystray 的消息循环线程里；在这里同步做 IPC 会把消息泵堵住，
        # 关机/注销时正是这种"消息泵不转"会被系统判成未响应。统一丢到后台线程。
        threading.Thread(
            target=self._run_action, args=(cmd, label), daemon=True, name="TrayAction"
        ).start()

    def _run_action(self, cmd: str, label: str) -> None:
        data = self._safe_call(cmd)
        if data is None:
            logger.warning("菜单动作「%s」失败：%s", label, self._error)
        else:
            logger.info("菜单动作「%s」成功：%s", label, data)

    def _on_toggle_autostart(self, icon=None, item=None) -> None:
        enabled = not autostart.setting_enabled()
        autostart.set_setting(enabled)
        if enabled:
            applied = autostart.ensure_registered()
        else:
            # 机器级注册项要管理员才能删，这里只清用户级项；下次登录被拉起时
            # 由 --autostart + 开关判断直接退出
            autostart.unregister()
            applied = False
        logger.info(
            "开机自启托盘 → %s（注册项已生效=%s）", "启用" if enabled else "禁用", applied
        )
        self._menu_signature = self._menu_state()
        self._icon.menu = self._build_menu()

    def _on_reconnect(self, icon=None, item=None) -> None:
        self._action("reconnect", "立即重连")

    def _on_start(self, icon=None, item=None) -> None:
        self._action("monitor.start", "启动监控")

    def _on_stop(self, icon=None, item=None) -> None:
        self._action("monitor.stop", "停止监控")

    def _on_logs(self, icon=None, item=None) -> None:
        threading.Thread(target=self._open_logs, daemon=True, name="TrayLogs").start()

    def _open_logs(self) -> None:
        data = self._safe_call("logs.path")
        if not data:
            logger.warning("打开日志目录失败：%s", self._error)
            messagebox.showerror("NetAnchor-Campus", self._error or "无法获取日志目录")
            return
        logger.info("打开日志目录：%s", data.get("path"))
        os.startfile(str(data["path"]))

    def _on_quit(self, icon=None, item=None) -> None:
        logger.info("用户退出托盘")
        self._stop.set()
        self._icon.stop()

    def _on_session_end(self) -> None:
        """系统要结束会话：停图标、让主线程的 icon.run() 返回后正常退出。"""
        self._stop.set()
        self._icon.stop()

    def _on_settings(self, icon=None, item=None) -> None:
        threading.Thread(target=self._settings_window, daemon=True).start()

    # ---- 设置窗口 ----
    def _settings_window(self) -> None:
        logger.info("打开设置窗口")
        try:
            current = self._call("config.get")["config"]
        except IpcError as exc:
            logger.warning("设置窗口读取配置失败：%s", exc)
            messagebox.showerror("NetAnchor-Campus", f"读取配置失败：{exc}")
            return

        account = current.get("account", {})
        candidate = current.get("portal", {}).get("candidates") or [{}]
        candidate = candidate[0]
        monitor = current.get("monitor", {})

        root = tk.Tk()
        root.title("NetAnchor-Campus 设置")
        root.resizable(False, False)

        # 按实际 DPI 取字号；窗口本身已声明 DPI 感知，不会再被位图拉伸
        dpi = root.winfo_fpixels("1i")
        root.tk.call("tk", "scaling", dpi / 72)
        base_size = max(9, round(dpi / 96 * 9))
        family = "Microsoft YaHei UI"

        style = ttk.Style(root)
        for theme in ("vista", "winnative", "clam"):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
        style.configure(".", font=(family, base_size))
        style.configure("Title.TLabel", font=(family, base_size + 4, "bold"))
        style.configure("Sub.TLabel", font=(family, base_size - 1), foreground="#6b7280")
        style.configure("Section.TLabel", font=(family, base_size, "bold"), foreground="#374151")
        style.configure("Hint.TLabel", font=(family, base_size - 1), foreground="#6b7280")
        style.configure("Field.TLabel", font=(family, base_size))

        # 窗口与标题栏图标
        art = _base_icon()
        photos = []
        if art is not None:
            try:
                from PIL import ImageTk

                window_icon = ImageTk.PhotoImage(art.resize((64, 64), Image.LANCZOS))
                photos.append(window_icon)
                root.iconphoto(True, window_icon)
                header_icon = ImageTk.PhotoImage(art.resize((56, 56), Image.LANCZOS))
                photos.append(header_icon)
            except Exception:  # noqa: BLE001 - 图标只是装饰，失败不影响设置
                header_icon = None
        else:
            header_icon = None

        pad = max(14, round(dpi / 96 * 14))
        outer = ttk.Frame(root, padding=(pad, pad, pad, pad))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        if header_icon is not None:
            ttk.Label(header, image=header_icon).pack(side="left", padx=(0, pad))
        titles = ttk.Frame(header)
        titles.pack(side="left", anchor="w")
        ttk.Label(titles, text="NetAnchor-Campus", style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="校园网断网自动重连", style="Sub.TLabel").pack(anchor="w")

        ttk.Separator(outer, orient="horizontal").pack(fill="x", pady=(pad, pad // 2))

        entries: dict[str, object] = {}

        def section(title: str) -> ttk.Frame:
            ttk.Label(outer, text=title, style="Section.TLabel").pack(anchor="w", pady=(pad // 2, 4))
            box = ttk.Frame(outer)
            box.pack(fill="x")
            return box

        def row(box: ttk.Frame, label: str, value, width: int = 30, show=None,
                expand: bool = True, suffix: str = ""):
            holder = ttk.Frame(box)
            holder.pack(fill="x", pady=2)
            ttk.Label(holder, text=label, width=10, style="Field.TLabel", anchor="w").pack(side="left")
            var = tk.StringVar(value=str(value))
            entry = ttk.Entry(holder, textvariable=var, width=width, show=show)
            if expand:
                entry.pack(side="left", fill="x", expand=True)
            else:
                entry.pack(side="left")
            if suffix:
                ttk.Label(holder, text=suffix, style="Field.TLabel").pack(side="left", padx=(6, 0))
            return var

        account_box = section("账号")
        entries["username"] = row(account_box, "学号", account.get("username", ""))
        entries["password"] = row(account_box, "新密码", "", show="*")

        carrier_holder = ttk.Frame(account_box)
        carrier_holder.pack(fill="x", pady=2)
        ttk.Label(carrier_holder, text="运营商", width=10, style="Field.TLabel", anchor="w").pack(side="left")
        carrier_var = tk.StringVar(value=account.get("carrier", "校园其他"))
        ttk.Combobox(
            carrier_holder, textvariable=carrier_var, values=list(CARRIER_SUFFIX),
            state="readonly", width=28,
        ).pack(side="left", fill="x", expand=True)
        entries["carrier"] = carrier_var

        portal_box = section("门户")
        entries["host"] = row(portal_box, "门户地址", candidate.get("host", ""))
        ports = ttk.Frame(portal_box)
        ports.pack(fill="x", pady=2)
        ttk.Label(ports, text="端口", width=10, style="Field.TLabel", anchor="w").pack(side="left")
        login_var = tk.StringVar(value=str(candidate.get("login_port", 80)))
        eportal_var = tk.StringVar(value=str(candidate.get("eportal_port", 801)))
        ttk.Label(ports, text="登录", style="Field.TLabel").pack(side="left")
        ttk.Entry(ports, textvariable=login_var, width=8).pack(side="left", padx=(4, pad))
        ttk.Label(ports, text="取码", style="Field.TLabel").pack(side="left")
        ttk.Entry(ports, textvariable=eportal_var, width=8).pack(side="left", padx=(4, 0))
        entries["login_port"] = login_var
        entries["eportal_port"] = eportal_var

        monitor_box = section("监控")
        entries["interval"] = row(
            monitor_box, "检测间隔", monitor.get("check_interval_seconds", 30),
            width=8, expand=False, suffix="秒",
        )
        ttk.Label(outer, text="断网后按退避阶梯重试，不会连续轰炸门户。", style="Hint.TLabel").pack(
            anchor="w", pady=(2, 0)
        )

        ttk.Separator(outer, orient="horizontal").pack(fill="x", pady=(pad, pad // 2))
        ttk.Label(
            outer,
            text="密码以本机加密存储，并与门户地址绑定；修改地址时必须重新输入密码。",
            style="Hint.TLabel",
            wraplength=max(320, round(dpi / 96 * 380)),
        ).pack(anchor="w")

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
                logger.info(
                    "保存配置：portal_changed=%s password_rebound=%s",
                    result.get("portal_changed"), result.get("password_rebound"),
                )
                messagebox.showinfo("NetAnchor-Campus", message)
                root.destroy()
            except IpcError as exc:
                text = str(exc)
                logger.warning("保存配置失败：%s", text)
                if "reauth_required" in text:
                    messagebox.showwarning(
                        "NetAnchor-Campus",
                        "门户地址已变更，请在“新密码”里重新输入一次密码后再保存。",
                    )
                else:
                    messagebox.showerror("NetAnchor-Campus", f"保存失败：{text}")
            except ValueError as exc:
                messagebox.showerror("NetAnchor-Campus", f"输入无效：{exc}")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(pad, 0))
        ttk.Button(buttons, text="保存", command=save).pack(side="right")
        ttk.Button(buttons, text="取消", command=root.destroy).pack(side="right", padx=(0, 8))

        root.update_idletasks()
        width = max(root.winfo_width(), max(320, round(dpi / 96 * 380)))
        height = root.winfo_height()
        x = (root.winfo_screenwidth() - width) // 2
        y = max(0, (root.winfo_screenheight() - height) // 3)
        root.geometry(f"{width}x{height}+{x}+{y}")
        root._photos = photos  # 防止图片被回收
        root.mainloop()

    # ---- 主循环 ----
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            # 单次刷新失败不能让轮询线程静默死掉：图标还在、状态永远不更新最难查
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                logger.exception("刷新托盘状态时出现未预期异常，继续轮询")
            self._stop.wait(POLL_SECONDS)

    def _setup(self, icon) -> None:
        """图标真正挂进托盘之后再刷一次：首屏就是当前状态色，不必等下一轮轮询。"""
        icon.visible = True
        try:
            self.refresh()
        except Exception:  # noqa: BLE001
            logger.exception("首轮刷新失败，继续轮询")

    def run(self) -> None:
        threading.Thread(target=self._poll_loop, daemon=True, name="TrayPoll").start()
        watch_session_end(self._on_session_end)
        # 自带 setup：pystray 在图标可见前丢掉的更新，由这一轮补回来
        self._icon.run(setup=self._setup)


def watch_session_end(on_end) -> None:
    """建一个隐藏的顶层窗口，专门接收关机/注销消息。

    pystray 的窗口过程只处理 DISPLAYCHANGE / STOP / NOTIFY / TASKBARCREATED，
    不响应 WM_QUERYENDSESSION / WM_ENDSESSION；进程不会在会话结束时主动退出，
    系统只能等超时再强杀，Application 日志里就留下"试图否决关机"。
    这里自己建窗口：查询阶段回 TRUE 放行，结束阶段立刻停图标退出。
    """

    def _loop() -> None:
        quit_message = win32gui.RegisterWindowMessage(TRAY_QUIT_MESSAGE)

        def handler(hwnd, msg, wparam, lparam):
            if msg == quit_message:
                # 安装/升级前由 CLI 投递；走和"用户退出"相同的清理路径
                logger.info("安装程序请求退出托盘")
                try:
                    on_end()
                except Exception:  # noqa: BLE001
                    logger.exception("退出前清理失败")
                win32gui.PostQuitMessage(0)
                return 0
            if msg == win32con.WM_QUERYENDSESSION:
                logger.info("系统请求结束会话（允许）")
                return 1
            if msg == win32con.WM_ENDSESSION:
                if wparam:
                    logger.info("会话即将结束，托盘退出")
                    try:
                        on_end()
                    except Exception:  # noqa: BLE001
                        logger.exception("会话结束时清理失败")
                win32gui.PostQuitMessage(0)
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        try:
            wc = win32gui.WNDCLASS()
            wc.lpszClassName = SESSION_WINDOW_CLASS
            wc.hInstance = win32api.GetModuleHandle(None)
            wc.lpfnWndProc = handler
            class_atom = win32gui.RegisterClass(wc)
            win32gui.CreateWindow(
                class_atom, SESSION_WINDOW_CLASS, 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None
            )
            win32gui.PumpMessages()
        except Exception:  # noqa: BLE001
            # 监视器起不来只是回归到旧行为，不该拦住托盘
            logger.exception("会话结束监视器启动失败（关机时可能被系统超时强杀）")

    threading.Thread(target=_loop, daemon=True, name="TraySessionEnd").start()


def tray_log_dir() -> Path:
    """托盘日志放用户目录：配置目录的 ACL 只给 SYSTEM/Administrators，托盘写不进去。"""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / LOG_DIR_NAME / "logs"


def _install_excepthooks() -> None:
    def _hook(exc_type, exc_value, exc_tb) -> None:
        logger.critical("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))

    def _thread_hook(args) -> None:
        thread_name = args.thread.name if args.thread is not None else "?"
        logger.critical(
            "线程 %s 未捕获异常", thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _hook
    threading.excepthook = _thread_hook


def main(argv: list[str] | None = None) -> int:
    enable_dpi_awareness()
    args = list(sys.argv[1:] if argv is None else argv)
    log_dir = tray_log_dir()
    try:
        setup_logging(log_dir, filename="tray.log")
    except OSError:
        # 日志目录不可写也不该拦住托盘，但要在启动参数里看得见
        pass
    _install_excepthooks()
    logger.info("托盘启动（版本 %s，日志 %s）", __version__, log_dir)

    if autostart.is_autostart_launch(args) and not autostart.setting_enabled():
        # 机器级注册项对所有用户生效，用户的"关闭"由这里兑现
        logger.info("本用户已关闭托盘开机自启，本次自启退出")
        return 0

    if not acquire_single_instance():
        logger.info("已有托盘实例在运行，本次启动退出")
        if not autostart.is_autostart_launch(args):
            # 手动双击才提示；开机自启的重复实例静默退出，免得每次登录弹一次框
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    None, "NetAnchor-Campus 托盘已经在运行。", "NetAnchor-Campus", 0x40
                )
            except Exception:  # noqa: BLE001
                pass
        return 0
    try:
        if autostart.setting_enabled():
            # 安装程序写过 HKLM 项就直接用；老版本没有时才补写 HKCU（自愈）
            registered = autostart.ensure_registered()
            logger.info("开机自启登记：%s", registered)
        else:
            autostart.unregister()
            logger.info("开机自启已关闭（机器级注册项由本开关否决）")
        TrayApp().run()
    except Exception:  # noqa: BLE001
        logger.exception("托盘异常退出")
        return 1
    finally:
        release_single_instance()
    logger.info("托盘已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
