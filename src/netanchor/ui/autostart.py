r"""托盘开机自启：机器级注册项（安装时写）+ 用户级开关。

`HKLM\...\Run` 里那条由安装程序写、卸载时删，所以任何本地用户登录都能拿到托盘；
每个用户仍可在菜单里关掉——关掉只写自己的开关，托盘以 `--autostart` 被拉起时
读到开关已关就直接退出，手动双击启动不受影响。
仅对打包后的 exe 生效（开发态运行 python 时不注册）。
"""
from __future__ import annotations

import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MACHINE_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SETTINGS_KEY = r"Software\NetAnchor-Campus"
VALUE_NAME = "NetAnchor-Campus Tray"
SETTING_NAME = "TrayAutostart"
# 标记"本次由开机自启拉起"，与用户手动双击区分
AUTOSTART_ARG = "--autostart"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def tray_exe() -> Path | None:
    if is_frozen():
        return Path(sys.executable).resolve()
    return None


def _open(path: str, access: int, hive=winreg.HKEY_CURRENT_USER):
    return winreg.OpenKey(hive, path, 0, access)


def is_registered(
    value_name: str = VALUE_NAME,
    key_path: str = RUN_KEY,
    hive=winreg.HKEY_CURRENT_USER,
) -> bool:
    try:
        with _open(key_path, winreg.KEY_READ, hive) as key:
            winreg.QueryValueEx(key, value_name)
        return True
    except OSError:
        return False


def register(
    exe: Path | None = None,
    value_name: str = VALUE_NAME,
    key_path: str = RUN_KEY,
    hive=winreg.HKEY_CURRENT_USER,
) -> bool:
    target = exe or tray_exe()
    if target is None:
        return False
    with winreg.CreateKeyEx(hive, key_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, f'"{target}"')
    return True


def unregister(
    value_name: str = VALUE_NAME,
    key_path: str = RUN_KEY,
    hive=winreg.HKEY_CURRENT_USER,
) -> None:
    try:
        with _open(key_path, winreg.KEY_SET_VALUE, hive) as key:
            winreg.DeleteValue(key, value_name)
    except OSError:
        pass


# ---- 机器级注册项：安装写、卸载删、全体用户生效 ----

def machine_entry(
    value_name: str = VALUE_NAME, key_path: str = MACHINE_RUN_KEY
) -> str | None:
    """返回 HKLM 里的自启命令行；没有或读不到时返回 None。"""
    try:
        with _open(key_path, winreg.KEY_READ, winreg.HKEY_LOCAL_MACHINE) as key:
            return str(winreg.QueryValueEx(key, value_name)[0])
    except OSError:
        return None


def register_machine(
    exe: Path | None = None,
    value_name: str = VALUE_NAME,
    key_path: str = MACHINE_RUN_KEY,
) -> bool:
    """写机器级自启项（带 `--autostart`）；需要管理员权限。"""
    target = exe or tray_exe()
    if target is None:
        return False
    with winreg.CreateKeyEx(
        winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(
            key, value_name, 0, winreg.REG_SZ, f'"{target}" {AUTOSTART_ARG}'
        )
    return True


def unregister_machine(
    value_name: str = VALUE_NAME, key_path: str = MACHINE_RUN_KEY
) -> None:
    unregister(value_name, key_path, hive=winreg.HKEY_LOCAL_MACHINE)


def setting_enabled(settings_key: str = SETTINGS_KEY, setting_name: str = SETTING_NAME) -> bool:
    """用户的开关，默认开启。"""
    try:
        with _open(settings_key, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, setting_name)
        return bool(value)
    except OSError:
        return True


def set_setting(enabled: bool, settings_key: str = SETTINGS_KEY, setting_name: str = SETTING_NAME) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, settings_key, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, setting_name, 0, winreg.REG_DWORD, 1 if enabled else 0)


def apply_setting(
    enabled: bool | None = None,
    settings_key: str = SETTINGS_KEY,
    setting_name: str = SETTING_NAME,
    value_name: str = VALUE_NAME,
    key_path: str = RUN_KEY,
) -> bool:
    """按开关同步注册表，返回生效后的状态。"""
    want = setting_enabled(settings_key, setting_name) if enabled is None else enabled
    if enabled is not None:
        set_setting(enabled, settings_key, setting_name)
    if want:
        return register(value_name=value_name, key_path=key_path)
    unregister(value_name=value_name, key_path=key_path)
    return False


def ensure_registered() -> bool:
    """保证至少有一处自启注册项。

    安装程序写的 HKLM 项对所有用户生效，存在就不用再写 HKCU——否则登录时会被
    拉起两份托盘。老版本（只写过 HKCU）走回退路径，自愈。
    """
    if machine_entry() is not None:
        return True
    return register()


def is_autostart_launch(argv: list[str]) -> bool:
    """本次是否由注册项拉起（带 `--autostart`）。"""
    return AUTOSTART_ARG in argv


def is_enabled() -> bool:
    """对用户呈现的状态：开关开着，且（安装版）有一处注册项。"""
    if not setting_enabled():
        return False
    if not is_frozen():
        return True
    return machine_entry() is not None or is_registered()
