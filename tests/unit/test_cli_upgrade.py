"""#4：升级在"停止服务"之后的任何失败都必须回到升级前的运行状态。"""
import shutil

import pytest

from netanchor import cli
from netanchor.cli import CliError


def _prepare(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / cli.SERVICE_EXE_NAME).write_text("new", encoding="utf-8")

    install = tmp_path / "install"
    install.mkdir()
    (install / "old.txt").write_text("old", encoding="utf-8")

    backup = tmp_path / "install.bak"
    monkeypatch.setattr(cli, "INSTALL_DIR", install)
    monkeypatch.setattr(cli, "BACKUP_DIR", backup)

    calls = {"stopped": 0, "started": 0}
    monkeypatch.setattr(cli, "_stop", lambda: calls.__setitem__("stopped", calls["stopped"] + 1))
    monkeypatch.setattr(cli, "_close_running_clients", lambda: None)
    monkeypatch.setattr(cli, "_start", lambda silent=False: calls.__setitem__("started", calls["started"] + 1))
    return src, install, backup, calls


def test_backup_failure_restores_service(tmp_path, monkeypatch):
    src, install, backup, calls = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "query_state", lambda *_a, **_k: "running")

    class BadShutil:
        @staticmethod
        def copytree(*_a, **_k):
            raise OSError("disk full")

        copy2 = staticmethod(shutil.copy2)
        rmtree = staticmethod(shutil.rmtree)

    monkeypatch.setattr(cli, "shutil", BadShutil)

    assert cli.upgrade(src) == 1
    assert (install / "old.txt").read_text(encoding="utf-8") == "old"  # 原文件未被破坏
    assert not backup.exists()
    assert calls["started"] == 1, "备份失败后必须把服务起回来"


def test_copy_failure_rolls_back_files_and_restarts(tmp_path, monkeypatch):
    src, install, backup, calls = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "query_state", lambda *_a, **_k: "running")

    state = {"failed": False}
    real_copy = cli._copy_tree

    def flaky_copy(source, target):
        if target == install and not state["failed"]:
            state["failed"] = True
            raise CliError("copy failed")
        return real_copy(source, target)

    monkeypatch.setattr(cli, "_copy_tree", flaky_copy)

    assert cli.upgrade(src) == 1
    assert (install / "old.txt").read_text(encoding="utf-8") == "old", "回滚后应恢复旧文件"
    assert calls["started"] == 1


def test_failure_keeps_stopped_state_when_service_was_stopped(tmp_path, monkeypatch):
    src, install, backup, calls = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "query_state", lambda *_a, **_k: "stopped")

    class BadShutil:
        @staticmethod
        def copytree(*_a, **_k):
            raise OSError("disk full")

        copy2 = staticmethod(shutil.copy2)
        rmtree = staticmethod(shutil.rmtree)

    monkeypatch.setattr(cli, "shutil", BadShutil)

    assert cli.upgrade(src) == 1
    assert calls["started"] == 0, "升级前服务是停止的，失败后不应擅自启动"
