"""安装器专用子命令的行为测试：全部注入假实现，不需要管理员与真实服务。"""
import sys
from pathlib import Path

import pytest

from netanchor import cli


class _Events:
    """记录调用顺序与参数。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def hook(self, name: str, result=None):
        def _fn(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return result

        return _fn

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def last(self, name: str):
        for call in reversed(self.calls):
            if call[0] == name:
                return call
        return None


class _FakeStore:
    base_dir = Path("C:/fake/ProgramData/NetAnchor-Campus")

    def init_template(self) -> Path:
        return self.base_dir / "config.json"


@pytest.fixture
def events(monkeypatch):
    record = _Events()
    monkeypatch.setattr(cli, "ConfigStore", lambda *a, **k: _FakeStore())
    for name in (
        "_ensure_service",
        "_set_failure_actions",
        "_remember_install_dir",
        "_register_tray_autostart",
        "_clear_tray_autostart",
        "_start",
        "_stop",
        "_close_running_clients",
        "_unregister_tray_autostart",
        "_delete_service",
        "_forget_install_dir",
    ):
        monkeypatch.setattr(cli, name, record.hook(name))
    return record


def _make_install(tmp_path: Path) -> Path:
    target = tmp_path / "install"
    target.mkdir()
    (target / cli.SERVICE_EXE_NAME).write_text("stub", encoding="utf-8")
    return target


def test_register_registers_service_before_starting(tmp_path, events):
    target = _make_install(tmp_path)

    assert cli.register(target) == 0

    assert events.names() == [
        "_ensure_service",
        "_set_failure_actions",
        "_remember_install_dir",
        "_register_tray_autostart",
        "_start",
    ]
    assert events.last("_ensure_service")[1][0] == target / cli.SERVICE_EXE_NAME


def test_register_without_start_skips_service_start(tmp_path, events):
    target = _make_install(tmp_path)

    assert cli.register(target, start=False) == 0

    assert "_start" not in events.names()


def test_register_without_autostart_clears_old_entries(tmp_path, events):
    target = _make_install(tmp_path)

    assert cli.register(target, autostart=False) == 0

    names = events.names()
    assert "_clear_tray_autostart" in names
    assert "_register_tray_autostart" not in names


def test_register_rejects_directory_without_service_exe(tmp_path, events):
    target = tmp_path / "empty"
    target.mkdir()

    assert cli.register(target) == 1
    assert events.calls == []


def test_prepare_stops_service_before_clearing_clients(tmp_path, events):
    target = _make_install(tmp_path)

    assert cli.prepare(target) == 0

    names = events.names()
    assert names.index("_stop") < names.index("_close_running_clients")
    assert events.last("_close_running_clients")[1][0] == target


def test_unregister_keeps_working_when_service_absent(tmp_path, events, monkeypatch):
    monkeypatch.setattr(cli, "query_state", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "installed_dir", lambda: tmp_path / "install")

    assert cli.unregister() == 0

    names = events.names()
    assert "_stop" not in names
    assert "_delete_service" not in names
    assert "_close_running_clients" in names
    assert "_unregister_tray_autostart" in names


def test_unregister_deletes_service_and_forgets_dir(tmp_path, events, monkeypatch):
    monkeypatch.setattr(cli, "query_state", lambda *_a, **_k: "stopped")
    monkeypatch.setattr(cli, "installed_dir", lambda: tmp_path / "install")

    assert cli.unregister() == 0

    names = events.names()
    assert "_delete_service" in names
    assert "_forget_install_dir" in names


def test_running_under_only_matches_install_dir(tmp_path, monkeypatch):
    target = tmp_path / "install"
    target.mkdir()
    ours = (target / cli.TRAY_EXE_NAME).resolve()
    elsewhere = (tmp_path / "other" / cli.TRAY_EXE_NAME).resolve()
    images = {100: ours, 200: elsewhere, 300: ours}
    monkeypatch.setattr(cli.win32process, "EnumProcesses", lambda: [100, 200, 300])

    found = cli._running_under(
        target, (cli.TRAY_EXE_NAME, cli.OCR_EXE_NAME), skip={300},
        image_of=lambda pid: images.get(pid),
    )

    assert found == {100: ours}, "只认安装目录内的镜像，且跳过调用方指定的 PID"


def test_clear_tray_autostart_fails_when_entry_survives(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    from netanchor.ui import autostart

    monkeypatch.setattr(autostart, "unregister_machine", lambda: None)
    monkeypatch.setattr(autostart, "unregister", lambda: None)
    monkeypatch.setattr(autostart, "machine_entry", lambda: '"x" --autostart')

    with pytest.raises(cli.CliError, match="托盘自启项未能清除"):
        cli._clear_tray_autostart()


def test_clear_tray_autostart_succeeds_when_entry_removed(monkeypatch):
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    from netanchor.ui import autostart

    monkeypatch.setattr(autostart, "unregister_machine", lambda: None)
    monkeypatch.setattr(autostart, "unregister", lambda: None)
    monkeypatch.setattr(autostart, "machine_entry", lambda: None)

    cli._clear_tray_autostart()


def test_install_uses_shared_register_path(tmp_path, events, monkeypatch):
    """install 与 register 必须共用同一段服务登记实现，不能各写一份。"""
    src = tmp_path / "pkg"
    src.mkdir()
    (src / cli.SERVICE_EXE_NAME).write_text("stub", encoding="utf-8")
    target = tmp_path / "install"

    monkeypatch.setattr(cli, "query_state", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_copy_tree", events.hook("_copy_tree"))

    assert cli.install(src, target_dir=target) == 0

    names = events.names()
    assert "_copy_tree" in names
    assert "_ensure_service" in names
    assert names.index("_copy_tree") < names.index("_ensure_service")