"""命令行：安装 / 升级 / 卸载 / 状态。均需要管理员权限。

安装与升级都要求"打包后的目录"——服务进程必须是 exe，
不能用 `python xxx.py install`（见 docs/待办.md 的 R2）。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import time
import winreg
from ctypes import wintypes
from pathlib import Path
from typing import Sequence

import pywintypes
import win32api
import win32con
import win32gui
import win32process
import win32service
import win32serviceutil

from .config_store import ConfigStore
from .service_main import SERVICE_DESCRIPTION, SERVICE_DISPLAY_NAME, SERVICE_NAME

SERVICE_EXE_NAME = "NetAnchor.Service.exe"
TRAY_EXE_NAME = "NetAnchor.Tray.exe"
OCR_EXE_NAME = "NetAnchor.Ocr.exe"
# 托盘的会话监视窗口：安装器通过它请托盘走自己的退出路径
SESSION_WINDOW_CLASS = "NetAnchorSessionEndWatcher"
TRAY_QUIT_MESSAGE = "NetAnchor-Tray-Quit"
# 仅作为"未指定 --dir 时"的默认值；实际安装位置以服务注册的 ImagePath 为准
DEFAULT_INSTALL_DIR = Path(r"C:\Program Files\NetAnchor-Campus")
APP_REG_KEY = r"SOFTWARE\NetAnchor-Campus"
REG_INSTALL_DIR = "InstallDir"

STATE_NAMES = {
    win32service.SERVICE_STOPPED: "stopped",
    win32service.SERVICE_START_PENDING: "start_pending",
    win32service.SERVICE_STOP_PENDING: "stop_pending",
    win32service.SERVICE_RUNNING: "running",
    win32service.SERVICE_CONTINUE_PENDING: "continue_pending",
    win32service.SERVICE_PAUSE_PENDING: "pause_pending",
    win32service.SERVICE_PAUSED: "paused",
}


class CliError(Exception):
    pass


# ---------------- 基础 ----------------

def default_source() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2] / "dist" / "NetAnchor-Campus"


def parse_image_path(raw: str) -> Path | None:
    """解析服务 ImagePath（可能带引号或额外参数）。

    注册表里的路径若含空格通常有引号；没有引号时以 `.exe` 结尾判定为完整路径，
    否则按空格切掉后面的参数。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith('"'):
        end = text.find('"', 1)
        if end == -1:
            return None
        return Path(text[1:end])
    if text.lower().endswith(".exe"):
        return Path(text)
    return Path(text.split(" ", 1)[0])


def _service_image_path(name: str = SERVICE_NAME) -> Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{name}") as key:
            value, _ = winreg.QueryValueEx(key, "ImagePath")
    except OSError:
        return None
    return parse_image_path(str(value))


def _remembered_install_dir() -> Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, APP_REG_KEY) as key:
            value, _ = winreg.QueryValueEx(key, REG_INSTALL_DIR)
    except OSError:
        return None
    return Path(str(value)) if value else None


def _remember_install_dir(path: Path) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, APP_REG_KEY, 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, REG_INSTALL_DIR, 0, winreg.REG_SZ, str(path))


def _forget_install_dir() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, APP_REG_KEY, 0, winreg.KEY_ALL_ACCESS) as key:
            winreg.DeleteValue(key, REG_INSTALL_DIR)
    except OSError:
        return


def installed_dir() -> Path:
    """已安装目录：以服务 ImagePath 为准，其次用安装时记录，最后回退默认值。"""
    image = _service_image_path()
    if image is not None:
        return image.parent
    remembered = _remembered_install_dir()
    if remembered is not None:
        return remembered
    return DEFAULT_INSTALL_DIR


def backup_dir_for(install_dir: Path) -> Path:
    return install_dir.with_name(install_dir.name + ".bak")


def _scm_manager(access: int):
    try:
        return win32service.OpenSCManager(None, None, access)
    except win32service.error as exc:
        raise CliError(f"无法访问服务管理器（需要管理员权限）: {exc}") from exc


def query_state(name: str = SERVICE_NAME) -> str | None:
    try:
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    except win32service.error:
        return None
    try:
        try:
            service = win32service.OpenService(scm, name, win32service.SERVICE_QUERY_STATUS)
        except win32service.error:
            return None
        try:
            status = win32service.QueryServiceStatus(service)[1]
        finally:
            win32service.CloseServiceHandle(service)
        return STATE_NAMES.get(status, str(status))
    finally:
        win32service.CloseServiceHandle(scm)


def _wait_for(target: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if query_state() == target:
            return True
        time.sleep(0.5)
    return False


def _remove_tree(path: Path, attempts: int = 12) -> None:
    """删除目录；服务刚停止时文件可能仍被占用，重试几次再报错。"""
    if not path.exists():
        return
    for _ in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except (PermissionError, OSError):
            time.sleep(0.5)
    raise CliError(f"删除失败，文件仍被占用: {path}")


def _process_image(pid: int) -> Path | None:
    """取进程镜像路径。无权限（SYSTEM 进程）或进程已退出时返回 None。"""
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except pywintypes.error:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
            int(handle), 0, buffer, ctypes.byref(size)
        )
        return Path(buffer.value) if ok else None
    except OSError:
        return None
    finally:
        handle.Close()


def _running_under(
    install_dir: Path,
    names: Sequence[str],
    skip: set[int],
    image_of=_process_image,
) -> dict[int, Path]:
    """安装目录下、文件名匹配的进程。

    只认镜像路径：别处同名的 exe（哪怕用户自己编译的）一律不碰。
    """
    wanted = {(install_dir / name).resolve() for name in names}
    found: dict[int, Path] = {}
    try:
        pids = win32process.EnumProcesses()
    except pywintypes.error:
        return found
    for pid in pids:
        if pid <= 0 or pid in skip:
            continue
        image = image_of(pid)
        if image is None:
            continue
        try:
            resolved = image.resolve()
        except OSError:
            continue
        if resolved in wanted:
            found[pid] = resolved
    return found


def _request_tray_exit(timeout: float = 5.0) -> None:
    """请托盘自己退出，而不是直接终止。

    先发注册消息 ``NetAnchor-Tray-Quit``；0.3.1 及更早的托盘不认识它，
    退回 ``WM_ENDSESSION``（等同于模拟注销，那条路径它们已经在用）。
    两种消息都只是"请求"，最终是否退出由调用方复查进程决定。
    """
    window = win32gui.FindWindow(SESSION_WINDOW_CLASS, None)
    if not window:
        return
    quit_message = win32gui.RegisterWindowMessage(TRAY_QUIT_MESSAGE)
    attempts = [(quit_message, 0), (win32con.WM_ENDSESSION, 1)]
    for message, wparam in attempts:
        if not message:
            continue
        try:
            win32gui.SendMessageTimeout(
                window, message, wparam, 0,
                win32con.SMTO_ABORTIFHUNG, int(timeout * 1000),
            )
        except pywintypes.error:
            continue
        return


def _close_running_clients(install_dir: Path | None = None, timeout: float = 5.0) -> None:
    """结束安装目录内的托盘与 OCR 进程——它们会占用被替换的 exe。

    顺序：先请它们正常退出（托盘走自己的退出路径），超时后只对残留下手，
    且只终止镜像路径落在安装目录里的进程。
    """
    target = (install_dir or installed_dir()).resolve()
    skip = {os.getpid(), os.getppid()}
    names = (TRAY_EXE_NAME, OCR_EXE_NAME)

    _request_tray_exit(timeout)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _running_under(target, names, skip):
            return
        time.sleep(0.2)

    for pid in _running_under(target, names, skip):
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
        except pywintypes.error:
            continue
        try:
            win32api.TerminateProcess(handle, 1)
        finally:
            handle.Close()
    time.sleep(0.5)

    left = _running_under(target, names, skip)
    if left:
        detail = ", ".join(f"{pid}={path}" for pid, path in left.items())
        raise CliError(f"仍有进程占用安装目录: {detail}")


def _copy_file(source: Path, target: Path, attempts: int = 40) -> None:
    """单文件复制；服务刚停止时目标文件可能仍被占用，重试后仍失败才报错。"""
    for _ in range(attempts):
        try:
            shutil.copy2(source, target)
            return
        except (PermissionError, OSError):
            time.sleep(0.25)
    raise CliError(f"复制失败，目标文件被占用: {target}")


def _copy_tree(source: Path, target: Path) -> None:
    if not source.exists():
        raise CliError(f"安装源不存在: {source}")
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            _copy_tree(item, destination)
        else:
            _copy_file(item, destination)


# ---------------- 服务操作 ----------------

def _create_service(exe_path: Path) -> None:
    scm = _scm_manager(win32service.SC_MANAGER_ALL_ACCESS)
    try:
        service = win32service.CreateService(
            scm,
            SERVICE_NAME,
            SERVICE_DISPLAY_NAME,
            win32service.SERVICE_ALL_ACCESS,
            win32service.SERVICE_WIN32_OWN_PROCESS,
            win32service.SERVICE_AUTO_START,
            win32service.SERVICE_ERROR_NORMAL,
            f'"{exe_path}"',
            None,
            0,
            None,
            None,
            None,
        )
        try:  # 部分 pywin32 版本的 CreateService 不接受描述参数
            win32service.ChangeServiceConfig2(
                service, win32service.SERVICE_CONFIG_DESCRIPTION, SERVICE_DESCRIPTION
            )
        except Exception:  # noqa: BLE001
            pass
        win32service.CloseServiceHandle(service)
    finally:
        win32service.CloseServiceHandle(scm)


def _ensure_service(exe_path: Path) -> None:
    """服务不存在就建，已存在就改指向——安装与升级共用。"""
    if query_state() is None:
        _create_service(exe_path)
        return
    scm = _scm_manager(win32service.SC_MANAGER_ALL_ACCESS)
    try:
        service = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
        try:
            win32service.ChangeServiceConfig(
                service,
                win32service.SERVICE_NO_CHANGE,
                win32service.SERVICE_AUTO_START,
                win32service.SERVICE_NO_CHANGE,
                f'"{exe_path}"',
                None,
                0,
                None,
                None,
                None,
                SERVICE_DISPLAY_NAME,
            )
        finally:
            win32service.CloseServiceHandle(service)
    finally:
        win32service.CloseServiceHandle(scm)


def _set_failure_actions() -> None:
    subprocess.run(
        [
            "sc.exe", "failure", SERVICE_NAME,
            "reset=", "86400",
            "actions=", "restart/5000/restart/10000/restart/30000",
        ],
        check=True, capture_output=True,
    )


def _start(silent: bool = False) -> None:
    try:
        win32serviceutil.StartService(SERVICE_NAME)
    except win32service.error as exc:
        # 1056 ERROR_SERVICE_ALREADY_RUNNING：register 会在服务还活着时被再次调用，
        # 那不是失败，后面的等待会确认它确实在跑
        if exc.winerror != 1056:
            raise CliError(f"启动服务失败: {exc}") from exc
    if not _wait_for("running"):
        raise CliError("服务未在 30 秒内进入 running")
    if not silent:
        print("服务已启动")


def _stop() -> None:
    state = query_state()
    if state is None or state == "stopped":
        return
    try:
        win32serviceutil.StopService(SERVICE_NAME)
    except win32service.error as exc:
        if exc.winerror != 1062:  # ERROR_SERVICE_NOT_ACTIVE
            raise CliError(f"停止服务失败: {exc}") from exc
    if not _wait_for("stopped"):
        raise CliError("服务未在 30 秒内停止")
    time.sleep(1)  # 给进程退出留一点余量，避免复制时文件仍被占用


def _delete_service() -> None:
    scm = _scm_manager(win32service.SC_MANAGER_ALL_ACCESS)
    try:
        service = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
        try:
            win32service.DeleteService(service)
        finally:
            win32service.CloseServiceHandle(service)
    finally:
        win32service.CloseServiceHandle(scm)


# ---------------- 命令 ----------------

def _register_tray_autostart(target: Path) -> None:
    """写机器级托盘自启项（HKLM），让任何本地用户登录都能拿到托盘。

    只有打包后的 CLI 才动机器级注册表：从源码运行（开发、测试）时不碰真实环境。
    托盘自己的菜单仍可给单个用户关掉；失败不阻断安装，但要看得见。
    """
    from .ui.autostart import register_machine, unregister

    if not getattr(sys, "frozen", False):
        print("托盘自启: 跳过（非打包运行）")
        return

    exe = target / TRAY_EXE_NAME
    try:
        if register_machine(exe):
            print(f"托盘自启: HKLM 注册项 -> {exe}")
        else:
            print("托盘自启: 跳过（未取到托盘路径）")
    except OSError as exc:
        print(f"警告: 托盘自启登记失败（托盘仍可手动启动）: {exc}", file=sys.stderr)
    # 迁移：老版本写过 HKCU，留着会和机器级项一起把托盘拉起两份。
    # 这里只能清当前账户（执行安装的管理员）那一份，其他用户的那份由托盘自启时静默去重。
    try:
        unregister()
    except OSError:
        pass


def _unregister_tray_autostart() -> None:
    from .ui.autostart import unregister_machine

    if not getattr(sys, "frozen", False):
        print("托盘自启: 跳过（非打包运行）")
        return
    unregister_machine()
    print("已移除托盘自启注册项")


def _clear_tray_autostart() -> None:
    """删掉机器级与当前用户级自启项；删不干净就报错，安装器据此判定失败。"""
    from .ui.autostart import machine_entry, register_machine, unregister, unregister_machine

    if not getattr(sys, "frozen", False):
        print("托盘自启: 跳过（非打包运行）")
        return
    unregister_machine()
    unregister()
    if machine_entry() is not None:
        raise CliError("托盘自启项未能清除（HKLM 拒绝写入，需管理员权限）")
    print("已移除托盘自启注册项")


def _register_install(target: Path, start: bool, autostart: bool) -> None:
    """把"一个目录已经就位"变成"服务可用"：服务、恢复动作、配置模板、自启、启动。

    install 与安装器的 register 共用这一段，服务生命周期只有一份实现。
    """
    _ensure_service(target / SERVICE_EXE_NAME)
    _set_failure_actions()
    store = ConfigStore()
    path = store.init_template()
    print(f"配置模板: {path}")
    _remember_install_dir(target)
    if autostart:
        _register_tray_autostart(target)
    else:
        _clear_tray_autostart()
    print(f"服务已登记: {SERVICE_NAME}（安装目录 {target}）")
    if start:
        _start(silent=True)


def install(source: Path | None = None, start: bool = True, target_dir: Path | None = None) -> int:
    if query_state() is not None:
        print("服务已存在，请使用 upgrade", file=sys.stderr)
        return 1
    src = (source or default_source()).resolve()
    exe = src / SERVICE_EXE_NAME
    if not exe.exists():
        print(f"未找到 {exe}", file=sys.stderr)
        return 1

    target = Path(target_dir) if target_dir else DEFAULT_INSTALL_DIR
    if not target.is_absolute():
        print(f"安装目录必须是绝对路径: {target}", file=sys.stderr)
        return 1
    if target.resolve() == src:
        print("安装目录不能与发布包目录相同", file=sys.stderr)
        return 1

    print(f"复制文件: {src} -> {target}")
    _copy_tree(src, target)
    _register_install(target, start=start, autostart=True)
    return 0


def upgrade(source: Path | None = None) -> int:
    if query_state() is None:
        print("服务未安装，请使用 install", file=sys.stderr)
        return 1
    src = (source or default_source()).resolve()
    if not (src / SERVICE_EXE_NAME).exists():
        print(f"未找到 {src / SERVICE_EXE_NAME}", file=sys.stderr)
        return 1

    was_running = query_state() == "running"
    target = installed_dir()
    backup = backup_dir_for(target)
    print(f"安装目录: {target}")

    # 等待子 CLI 的父进程仍会锁住安装目录中的自身影像。
    if getattr(sys, "frozen", False):
        running_from = Path(sys.executable).resolve().parent
        if running_from == target.resolve():
            raise CliError(
                "不能从安装目录内运行 upgrade（会锁住自身）。请退出当前 CLI 后，"
                f'运行发布包中的 "{src / "NetAnchor.Cli.exe"}" upgrade --source "{src}"'
            )

    print("停止服务")
    _stop()
    print("结束托盘与 OCR 进程")
    _close_running_clients()

    # 从停止服务开始的一切失败都必须能回到"升级前的运行状态"
    backup_ready = False
    try:
        if backup.exists():
            _remove_tree(backup)
        shutil.copytree(target, backup)
        backup_ready = True
        print(f"已备份到 {backup}")

        # 干净安装：先删旧目录再复制。只覆盖不删除会把上一版独有的文件留下
        # （例如根目录的 pywintypes314.dll，会让旧缺陷继续生效）。
        _remove_tree(target)
        _copy_tree(src, target)
        # 安装目录可能换过，注册项跟着指向新路径（换目录后仍能自启）
        _register_tray_autostart(target)
        _start()
    except Exception as exc:  # noqa: BLE001
        print(f"升级失败，正在恢复: {exc}", file=sys.stderr)
        _restore_previous_install(target, backup, backup_ready, was_running)
        return 1
    print("升级完成")
    return 0


def _restore_previous_install(target: Path, backup: Path, backup_ready: bool, was_running: bool) -> None:
    """升级失败后的恢复：有备份就还原文件，再把服务起回升级前的状态。"""
    if backup_ready and backup.exists():
        try:
            _remove_tree(target)
        except CliError as exc:
            print(f"恢复前删除失败: {exc}", file=sys.stderr)
        try:
            _copy_tree(backup, target)
            print("已还原升级前的文件")
        except CliError as exc:
            print(f"还原文件失败: {exc}", file=sys.stderr)

    if not was_running:
        print("服务升级前已停止，保持停止状态")
        return
    try:
        _start(silent=True)
        print("已恢复升级前的服务")
    except Exception as exc:  # noqa: BLE001
        print(f"恢复服务失败: {exc}", file=sys.stderr)


def uninstall(purge: bool = False) -> int:
    state = query_state()
    if state is not None:
        print("停止服务")
        _stop()

    print("结束托盘与 OCR 进程")
    _close_running_clients()

    _unregister_tray_autostart()

    target = installed_dir()
    backup = backup_dir_for(target)

    # 先删文件、后删服务：文件删不掉时服务仍然存在（已停止），状态可恢复
    failures: list[str] = []
    for path in (target, backup):
        if not path.exists():
            continue
        try:
            _remove_tree(path)
            print(f"已删除 {path}")
        except CliError as exc:
            failures.append(str(exc))
            print(f"删除失败: {exc}", file=sys.stderr)

    if failures:
        print(
            "安装目录未清理干净：服务保留（已停止）。关闭占用文件的程序后可重新执行 uninstall。",
            file=sys.stderr,
        )
        return 1

    if state is not None:
        _delete_service()
        _forget_install_dir()
        print("服务已删除")

    return 0 if _finish_config(purge) else 1


def _finish_config(purge: bool) -> bool:
    """收尾：按需删除配置与日志。返回是否成功。"""
    store = ConfigStore()
    if not purge:
        print(f"配置与日志保留在: {store.base_dir}（如需删除加 --purge）")
        return True
    try:
        _remove_tree(store.base_dir)
    except CliError as exc:
        print(f"配置目录清理失败: {exc}", file=sys.stderr)
        print(f"仍存在: {store.base_dir}", file=sys.stderr)
        return False
    if store.base_dir.exists():
        print(f"配置目录仍存在，未完全删除: {store.base_dir}", file=sys.stderr)
        return False
    print(f"已删除配置与日志: {store.base_dir}")
    return True


# ---------------- 安装器专用命令 ----------------

def prepare(install_dir: Path | None = None) -> int:
    """升级前清场：停服务、请托盘与 OCR 退出。不做任何文件操作。

    失败即返回非零，调用方必须据此中止，不能继续替换文件。
    """
    target = (Path(install_dir) if install_dir else installed_dir()).resolve()
    print(f"安装目录: {target}")
    print("停止服务")
    _stop()
    print("结束托盘与 OCR 进程")
    _close_running_clients(target)
    print("已清场")
    return 0


def register(target_dir: Path, start: bool = True, autostart: bool = True) -> int:
    """只登记、不复制：文件由安装器铺好，服务与自启由此处建立或修正。"""
    target = Path(target_dir).resolve()
    if not (target / SERVICE_EXE_NAME).exists():
        print(f"未找到 {target / SERVICE_EXE_NAME}", file=sys.stderr)
        return 1
    print(f"安装目录: {target}")
    _register_install(target, start=start, autostart=autostart)
    print("登记完成")
    return 0


def unregister(purge: bool = False) -> int:
    """拆除服务与登记，但不动安装目录里的文件（文件归安装器删）。"""
    target = installed_dir()
    state = query_state()
    if state is not None:
        print("停止服务")
        _stop()
    print("结束托盘与 OCR 进程")
    _close_running_clients(target)
    _unregister_tray_autostart()
    if state is not None:
        _delete_service()
        _forget_install_dir()
        print("服务已删除")
    return 0 if _finish_config(purge) else 1


def status() -> int:
    state = query_state()
    store = ConfigStore()
    target = installed_dir()
    print(f"服务: {state or '未安装'}")
    print(f"安装目录: {target} ({'存在' if target.exists() else '不存在'})")
    print(f"配置文件: {store.path} ({'存在' if store.path.exists() else '不存在'})")
    print(f"日志目录: {store.base_dir / 'logs'}")
    return 0 if state is not None else 1


# ---------------- 入口 ----------------

def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(prog="NetAnchor.Cli", description="NetAnchor-Campus 安装与维护")
    parser.add_argument("--version", action="version", version=f"NetAnchor.Cli {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="安装服务")
    p_install.add_argument("--source", type=Path, default=None, help="打包产物目录")
    p_install.add_argument("--dir", type=Path, default=None, dest="target_dir",
                           help=f"安装目录（默认 {DEFAULT_INSTALL_DIR}）")
    p_install.add_argument("--no-start", action="store_true", help="安装后不启动")

    p_upgrade = sub.add_parser(
        "upgrade", help="升级并保留配置（安装目录跟随服务注册路径，不接受 --dir）"
    )
    p_upgrade.add_argument("--source", type=Path, default=None, help="打包产物目录")

    p_uninstall = sub.add_parser("uninstall", help="卸载服务")
    p_uninstall.add_argument("--purge", action="store_true", help="同时删除配置与日志")

    p_prepare = sub.add_parser(
        "prepare", help="安装器用：停止服务并结束托盘/OCR（升级前清场）"
    )
    p_prepare.add_argument("--install-dir", type=Path, default=None, dest="install_dir",
                           help="安装目录（默认跟随服务注册路径）")

    p_register = sub.add_parser(
        "register", help="安装器用：只登记服务与自启，不复制文件"
    )
    p_register.add_argument("--dir", type=Path, required=True, dest="target_dir",
                            help="已被安装器铺好文件的安装目录")
    p_register.add_argument("--no-start", action="store_true", help="登记后不启动服务")
    p_register.add_argument("--no-autostart", action="store_true",
                            help="清掉托盘自启项（不写入）")

    p_unregister = sub.add_parser(
        "unregister", help="安装器用：拆除服务与自启，不删除文件"
    )
    p_unregister.add_argument("--purge", action="store_true", help="同时删除配置与日志")

    sub.add_parser("status", help="查看安装状态")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "install":
            return install(args.source, start=not args.no_start, target_dir=args.target_dir)
        if args.command == "upgrade":
            return upgrade(args.source)
        if args.command == "uninstall":
            return uninstall(purge=args.purge)
        if args.command == "prepare":
            return prepare(args.install_dir)
        if args.command == "register":
            return register(
                args.target_dir, start=not args.no_start, autostart=not args.no_autostart
            )
        if args.command == "unregister":
            return unregister(purge=args.purge)
        return status()
    except CliError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
