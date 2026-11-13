"""#5：卸载要先关闭占用文件的客户端，且删除失败时保留可恢复状态。"""
import pytest

from netanchor import cli
from netanchor.cli import CliError


def _prepare(tmp_path, monkeypatch, state="running"):
    install = tmp_path / "install"
    install.mkdir()
    (install / "NetAnchor.Tray.exe").write_text("x", encoding="utf-8")
    backup = tmp_path / "install.bak"
    backup.mkdir()

    monkeypatch.setattr(cli, "installed_dir", lambda: install)
    monkeypatch.setattr(cli, "backup_dir_for", lambda _target: backup)
    monkeypatch.setattr(cli, "_forget_install_dir", lambda: None)
    monkeypatch.setattr(cli, "query_state", lambda *_a, **_k: state)

    events = []
    monkeypatch.setattr(cli, "_stop", lambda: events.append("stop"))
    monkeypatch.setattr(cli, "_close_running_clients", lambda: events.append("close"))
    monkeypatch.setattr(cli, "_delete_service", lambda: events.append("delete_service"))
    return install, backup, events


def test_clients_closed_before_deleting_files(tmp_path, monkeypatch):
    install, backup, events = _prepare(tmp_path, monkeypatch)
    real_remove = cli._remove_tree
    monkeypatch.setattr(
        cli, "_remove_tree",
        lambda path: (events.append(f"remove:{path.name}"), real_remove(path)),
    )

    assert cli.uninstall() == 0
    assert events.index("stop") < events.index("close")
    assert events.index("close") < events.index(f"remove:{install.name}")
    # 服务在文件删除成功之后才删除
    assert events.index(f"remove:{install.name}") < events.index("delete_service")
    assert not install.exists()
    assert not backup.exists()


def test_delete_failure_keeps_service_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    install, backup, events = _prepare(tmp_path, monkeypatch)

    def always_fail(_path):
        raise CliError("文件被占用")

    monkeypatch.setattr(cli, "_remove_tree", always_fail)

    assert cli.uninstall() == 1
    assert "delete_service" not in events, "文件没删干净时不应删除服务，保留可恢复状态"
    assert install.exists()
    assert "未清理干净" in capsys.readouterr().err


def test_uninstall_without_service_still_cleans_files(tmp_path, monkeypatch):
    install, backup, events = _prepare(tmp_path, monkeypatch, state=None)
    real_remove = cli._remove_tree
    monkeypatch.setattr(
        cli, "_remove_tree",
        lambda path: (events.append(f"remove:{path.name}"), real_remove(path)),
    )

    assert cli.uninstall() == 0
    assert "stop" not in events
    assert "delete_service" not in events
    assert not install.exists()

def _quiet_uninstall(monkeypatch, tmp_path, base):
    monkeypatch.setattr(cli, "query_state", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "installed_dir", lambda: tmp_path / "install")
    monkeypatch.setattr(cli, "_close_running_clients", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_unregister_tray_autostart", lambda: None)

    class Store:
        def __init__(self) -> None:
            self.base_dir = base

        @property
        def path(self):
            return base / "config.json"

    monkeypatch.setattr(cli, "ConfigStore", lambda *a, **k: Store())


def test_purge_removes_config_and_logs(tmp_path, monkeypatch):
    base = tmp_path / "ProgramData"
    base.mkdir()
    (base / "config.json").write_text("{}", encoding="utf-8")
    _quiet_uninstall(monkeypatch, tmp_path, base)

    assert cli.uninstall(purge=True) == 0
    assert not base.exists(), "--purge 应删掉配置与日志目录"


def test_without_purge_keeps_config(tmp_path, monkeypatch):
    base = tmp_path / "ProgramData"
    base.mkdir()
    (base / "config.json").write_text("{}", encoding="utf-8")
    _quiet_uninstall(monkeypatch, tmp_path, base)

    assert cli.uninstall(purge=False) == 0
    assert base.exists(), "默认必须保留配置"


def test_purge_failure_returns_nonzero(tmp_path, monkeypatch):
    base = tmp_path / "ProgramData"
    base.mkdir()
    _quiet_uninstall(monkeypatch, tmp_path, base)
    monkeypatch.setattr(
        cli, "_remove_tree", lambda *_a, **_k: (_ for _ in ()).throw(cli.CliError("拒绝访问"))
    )

    assert cli.uninstall(purge=True) == 1
