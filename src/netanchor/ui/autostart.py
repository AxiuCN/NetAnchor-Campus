"""托盘开机自启：写 HKCU\\...\\Run，由托盘程序自己管理。

作用域是当前用户：服务随开机启动，托盘随该用户登录启动。
仅对打包后的 exe 生效（开发态运行 python 时不注册）。
"""
from __future__ import annotations

import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SETTINGS_KEY = r"Software\NetAnchor-Campus"
VALUE_NAME = "NetAnchor-Campus Tray"
SETTING_NAME = "TrayAutostart"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def tray_exe() -> Path | None:
    if is_frozen():
        return Path(sys.executable).resolve()
    return None


def _open(path: str, access: int):
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, access)


def is_registered(value_name: str = VALUE_NAME, key_path: str = RUN_KEY) -> bool:
    try:
        with _open(key_path, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, value_name)
        return True
    except OSError:
        return False


def register(exe: Path | None = None, value_name: str = VALUE_NAME, key_path: str = RUN_KEY) -> bool:
    target = exe or tray_exe()
    if target is None:
        return False
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, f'"{target}"')
    return True


def unregister(value_name: str = VALUE_NAME, key_path: str = RUN_KEY) -> None:
    try:
        with _open(key_path, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)
    except OSError:
        pass


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


def is_enabled() -> bool:
    """对用户呈现的状态：开关开着，且（安装版）注册项存在。"""
    if not setting_enabled():
        return False
    if not is_frozen():
        return True
    return is_registered()
