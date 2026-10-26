"""命令行：安装 / 升级 / 卸载 / 状态。均需要管理员权限。

安装与升级都要求"打包后的目录"——服务进程必须是 exe，
不能用 `python xxx.py install`（见 docs/architecture.md §14 R2）。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

import win32service
import win32serviceutil

from .config_store import ConfigStore
from .service_main import SERVICE_DESCRIPTION, SERVICE_DISPLAY_NAME, SERVICE_NAME

SERVICE_EXE_NAME = "NetAnchor.Service.exe"
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


def _close_running_clients() -> None:
    """升级前结束托盘与 OCR 工作进程——它们会占用被替换的 exe。"""
    for image in ("NetAnchor.Tray.exe", "NetAnchor.Ocr.exe"):
        subprocess.run(["taskkill", "/f", "/im", image], capture_output=True)
    time.sleep(1)


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
    store = ConfigStore()
    path = store.init_template()
    print(f"配置模板: {path}")

    _create_service(target / SERVICE_EXE_NAME)
    _set_failure_actions()
    _remember_install_dir(target)
    print(f"服务已创建: {SERVICE_NAME}（安装目录 {target}）")
    if start:
        _start()
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

        _copy_tree(src, target)
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

    store = ConfigStore()
    if purge:
        try:
            _remove_tree(store.base_dir)
        except CliError as exc:
            print(f"配置目录清理失败: {exc}", file=sys.stderr)
            print(f"仍存在: {store.base_dir}", file=sys.stderr)
            return 1
        if store.base_dir.exists():
            print(f"配置目录仍存在，未完全删除: {store.base_dir}", file=sys.stderr)
            return 1
        print(f"已删除配置与日志: {store.base_dir}")
    else:
        print(f"配置与日志保留在: {store.base_dir}（如需删除加 --purge）")
    return 0


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
        return status()
    except CliError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
