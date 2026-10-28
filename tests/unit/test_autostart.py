"""托盘开机自启：注册项读写与会话级开关。"""
import uuid
import winreg
from pathlib import Path

import pytest

from netanchor.ui import autostart


@pytest.fixture()
def keys():
    suffix = uuid.uuid4().hex
    settings = rf"Software\NetAnchor-Test-{suffix}"
    run_key = settings + r"\Run"
    value_name = "Test Tray"
    yield run_key, settings, value_name
    for path in (run_key, settings):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass


def _read(run_key: str, value_name: str) -> str:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key) as key:
        return winreg.QueryValueEx(key, value_name)[0]


def test_register_and_unregister(keys):
    run_key, _settings, value_name = keys
    exe = Path(r"C:\Apps\NetAnchor.Tray.exe")

    assert autostart.register(exe, value_name=value_name, key_path=run_key) is True
    assert autostart.is_registered(value_name, run_key) is True
    assert _read(run_key, value_name) == f'"{exe}"'

    autostart.unregister(value_name, run_key)
    assert autostart.is_registered(value_name, run_key) is False


def test_register_without_frozen_returns_false(keys):
    run_key, _settings, value_name = keys
    # 开发态（非 frozen）没有可注册的 exe
    assert autostart.register(None, value_name=value_name, key_path=run_key) is False


def test_setting_defaults_to_enabled(keys):
    _run_key, settings, _value = keys
    assert autostart.setting_enabled(settings, "TrayAutostart") is True


def test_apply_setting_disables_and_removes_entry(keys):
    run_key, settings, value_name = keys
    autostart.register(Path(r"C:\Apps\Tray.exe"), value_name=value_name, key_path=run_key)

    state = autostart.apply_setting(
        enabled=False,
        settings_key=settings,
        setting_name="TrayAutostart",
        value_name=value_name,
        key_path=run_key,
    )
    assert state is False
    assert autostart.setting_enabled(settings, "TrayAutostart") is False
    assert autostart.is_registered(value_name, run_key) is False


def test_apply_setting_enables_again(keys):
    run_key, settings, value_name = keys
    autostart.set_setting(False, settings, "TrayAutostart")

    state = autostart.apply_setting(
        enabled=True,
        settings_key=settings,
        setting_name="TrayAutostart",
        value_name=value_name,
        key_path=run_key,
    )
    # 非 frozen 环境拿不到 exe，注册不会真的发生；开关本身应被打开
    assert autostart.setting_enabled(settings, "TrayAutostart") is True
    assert state is False
