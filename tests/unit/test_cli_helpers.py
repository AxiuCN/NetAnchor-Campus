from pathlib import Path

import pytest

from netanchor import cli
from netanchor.cli import (
    _copy_tree,
    _remove_tree,
    build_parser,
    default_source,
    installed_dir,
    parse_image_path,
    query_state,
)


def test_copy_tree(tmp_path):
    source = tmp_path / "src"
    (source / "sub").mkdir(parents=True)
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "sub" / "b.txt").write_text("b", encoding="utf-8")

    target = tmp_path / "dst"
    _copy_tree(source, target)

    assert (target / "a.txt").read_text(encoding="utf-8") == "a"
    assert (target / "sub" / "b.txt").read_text(encoding="utf-8") == "b"


def test_copy_tree_missing_source_raises(tmp_path):
    import pytest

    from netanchor.cli import CliError

    with pytest.raises(CliError):
        _copy_tree(tmp_path / "absent", tmp_path / "dst")


def test_remove_tree(tmp_path):
    target = tmp_path / "x"
    (target / "y").mkdir(parents=True)
    (target / "y" / "z.txt").write_text("z", encoding="utf-8")
    _remove_tree(target)
    assert not target.exists()


def test_remove_tree_missing_is_noop(tmp_path):
    _remove_tree(tmp_path / "absent")


def test_parser_install():
    args = build_parser().parse_args(["install", "--source", "C:/dist", "--no-start"])
    assert args.command == "install"
    assert args.source == Path("C:/dist")
    assert args.no_start is True


def test_parser_install_dir():
    args = build_parser().parse_args(["install", "--dir", r"D:\Program Files\NetAnchor-Campus"])
    assert args.target_dir == Path(r"D:\Program Files\NetAnchor-Campus")


def test_parse_image_path_quoted_and_plain():
    assert parse_image_path('"C:\\Apps\\NetAnchor.Service.exe"') == Path(r"C:\Apps\NetAnchor.Service.exe")
    assert parse_image_path('"C:\\Apps\\NetAnchor.Service.exe" -run') == Path(r"C:\Apps\NetAnchor.Service.exe")
    assert parse_image_path(r"D:\Program Files\X\NetAnchor.Service.exe") == Path(
        r"D:\Program Files\X\NetAnchor.Service.exe"
    )
    assert parse_image_path("") is None


def test_installed_dir_prefers_service_image(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_service_image_path", lambda *_a, **_k: tmp_path / "svc" / cli.SERVICE_EXE_NAME)
    monkeypatch.setattr(cli, "_remembered_install_dir", lambda: tmp_path / "remembered")
    assert installed_dir() == tmp_path / "svc"


def test_installed_dir_falls_back_to_remembered(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_service_image_path", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_remembered_install_dir", lambda: tmp_path / "remembered")
    assert installed_dir() == tmp_path / "remembered"


def test_installed_dir_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(cli, "_service_image_path", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_remembered_install_dir", lambda: None)
    assert installed_dir() == cli.DEFAULT_INSTALL_DIR


def test_install_custom_dir(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / cli.SERVICE_EXE_NAME).write_text("exe", encoding="utf-8")
    target = tmp_path / "custom" / "NetAnchor-Campus"

    created = {}

    class FakeStore:
        def init_template(self):
            return tmp_path / "config.json"

    monkeypatch.setattr(cli, "query_state", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "ConfigStore", FakeStore)
    monkeypatch.setattr(cli, "_set_failure_actions", lambda: None)
    monkeypatch.setattr(cli, "_remember_install_dir", lambda path: created.__setitem__("remembered", path))
    monkeypatch.setattr(cli, "_create_service", lambda exe: created.__setitem__("service", exe))
    monkeypatch.setattr(cli, "_start", lambda silent=False: None)

    assert cli.install(source, start=True, target_dir=target) == 0
    assert (target / cli.SERVICE_EXE_NAME).exists()
    assert created["service"] == target / cli.SERVICE_EXE_NAME
    assert created["remembered"] == target


def test_install_rejects_relative_dir(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / cli.SERVICE_EXE_NAME).write_text("exe", encoding="utf-8")
    monkeypatch.setattr(cli, "query_state", lambda *_a, **_k: None)

    assert cli.install(source, target_dir=Path("relative-dir")) == 1


def test_parser_upgrade_and_uninstall():
    upgrade_args = build_parser().parse_args(["upgrade"])
    assert upgrade_args.command == "upgrade"
    purge_args = build_parser().parse_args(["uninstall", "--purge"])
    assert purge_args.purge is True


def test_parser_status():
    assert build_parser().parse_args(["status"]).command == "status"


def test_query_state_for_absent_service():
    assert query_state("NetAnchorDefinitelyNotInstalled") is None


def test_default_source_returns_path():
    assert isinstance(default_source(), Path)
