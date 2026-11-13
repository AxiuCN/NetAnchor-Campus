"""_start 在服务已经运行时不能报错——register 会被重复调用。"""

import pytest

from netanchor import cli


def test_start_tolerates_already_running_service(monkeypatch):
    def already_running(_name):
        raise cli.win32service.error(1056, "StartService", "服务已在运行")

    monkeypatch.setattr(cli.win32serviceutil, "StartService", already_running)
    monkeypatch.setattr(cli, "_wait_for", lambda *_a, **_k: True)

    cli._start(silent=True)  # 不应抛异常


def test_start_still_fails_on_other_errors(monkeypatch):
    def denied(_name):
        raise cli.win32service.error(5, "StartService", "拒绝访问")

    monkeypatch.setattr(cli.win32serviceutil, "StartService", denied)

    with pytest.raises(cli.CliError, match="启动服务失败"):
        cli._start(silent=True)


def test_register_is_idempotent_when_service_running(tmp_path, monkeypatch):
    target = tmp_path / "install"
    target.mkdir()
    (target / cli.SERVICE_EXE_NAME).write_text("stub", encoding="utf-8")

    for name in (
        "_ensure_service", "_set_failure_actions", "_remember_install_dir",
        "_register_tray_autostart",
    ):
        monkeypatch.setattr(cli, name, lambda *a, **k: None)
    monkeypatch.setattr(cli, "ConfigStore", lambda *a, **k: type(
        "S", (), {"init_template": lambda self: target / "config.json"}
    )())
    monkeypatch.setattr(
        cli.win32serviceutil, "StartService",
        lambda _name: (_ for _ in ()).throw(
            cli.win32service.error(1056, "StartService", "服务已在运行")
        ),
    )
    monkeypatch.setattr(cli, "_wait_for", lambda *_a, **_k: True)

    assert cli.register(target) == 0