from pathlib import Path

from netanchor.cli import _copy_tree, _remove_tree, build_parser, default_source, query_state


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
