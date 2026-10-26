"""#6：`--purge` 清理失败必须返回非零，且不能声称已删除。"""
import pytest

from netanchor import cli
from netanchor.cli import CliError
from netanchor.config_store import ConfigStore


def _prepare(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "service.log").write_text("log", encoding="utf-8")

    store = ConfigStore(data_dir, apply_acl=False)
    monkeypatch.setattr(cli, "ConfigStore", lambda: store)
    monkeypatch.setattr(cli, "installed_dir", lambda: tmp_path / "install-absent")
    monkeypatch.setattr(cli, "backup_dir_for", lambda _target: tmp_path / "backup-absent")
    monkeypatch.setattr(cli, "query_state", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "ConfigStore", lambda: store)
    monkeypatch.setattr(cli, "_close_running_clients", lambda: None)
    return data_dir


def test_purge_success_removes_directory(tmp_path, monkeypatch, capsys):
    data_dir = _prepare(tmp_path, monkeypatch)

    assert cli.uninstall(purge=True) == 0
    assert not data_dir.exists()
    out = capsys.readouterr().out
    assert "已删除配置与日志" in out


def test_purge_failure_returns_nonzero_and_keeps_directory(tmp_path, monkeypatch, capsys):
    data_dir = _prepare(tmp_path, monkeypatch)

    def always_fail(_path):
        raise CliError("文件被占用")

    monkeypatch.setattr(cli, "_remove_tree", always_fail)

    assert cli.uninstall(purge=True) == 1
    assert data_dir.exists()
    captured = capsys.readouterr()
    assert "清理失败" in captured.err
    assert "已删除配置与日志" not in captured.out, "失败时不得声称已删除"


def test_purge_verifies_directory_gone(tmp_path, monkeypatch, capsys):
    data_dir = _prepare(tmp_path, monkeypatch)
    # 模拟"删除函数不报错但也没删干净"
    monkeypatch.setattr(cli, "_remove_tree", lambda _path: None)

    assert cli.uninstall(purge=True) == 1
    assert data_dir.exists()
    assert "仍存在" in capsys.readouterr().err


def test_without_purge_keeps_directory(tmp_path, monkeypatch, capsys):
    data_dir = _prepare(tmp_path, monkeypatch)

    assert cli.uninstall(purge=False) == 0
    assert data_dir.exists()
    assert "配置与日志保留在" in capsys.readouterr().out
