import json

import pytest

from netanchor.config import (
    CARRIER_SUFFIX,
    PORTAL_PLACEHOLDER,
    Config,
    PortalCandidate,
)


def test_defaults_are_placeholders():
    cfg = Config()
    assert cfg.config_version == 1
    assert cfg.portal.candidates[0].host == PORTAL_PLACEHOLDER
    assert cfg.monitor.check_interval_seconds == 30
    assert cfg.monitor.max_captcha_attempts == 3
    assert cfg.ocr.mode == "subprocess"


def test_validate_flags_placeholder_host():
    errors = Config().validate()
    assert any("未填写 host" in item for item in errors)


def test_validate_ok_with_real_host():
    cfg = Config()
    cfg.portal.candidates = [PortalCandidate(name="主用", host="10.0.0.1")]
    cfg.account.username = "user"
    cfg.account.password = "secret"
    assert cfg.validate() == []


def test_carrier_suffix_and_effective_username():
    cfg = Config()
    cfg.account.username = "20240001"
    for carrier, suffix in CARRIER_SUFFIX.items():
        cfg.account.carrier = carrier
        assert cfg.effective_username == f"20240001{suffix}"


def test_unknown_carrier_rejected():
    cfg = Config()
    cfg.portal.candidates = [PortalCandidate(host="10.0.0.1")]
    cfg.account.carrier = "不存在的运营商"
    assert any("未知运营商" in item for item in cfg.validate())


def test_password_never_serialized(tmp_path):
    cfg = Config()
    cfg.account.username = "u"
    cfg.account.password = "p@ss"
    cfg.account.password_protected = "enc"
    path = tmp_path / "config.json"
    cfg.save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "password" not in raw["account"]
    assert raw["account"]["password_protected"] == "enc"


def test_save_load_roundtrip(tmp_path):
    cfg = Config()
    cfg.portal.candidates = [PortalCandidate(name="A", host="10.0.0.1", login_port=8080, eportal_port=8081)]
    cfg.account.username = "u"
    cfg.account.carrier = "校园电信"
    cfg.monitor.check_interval_seconds = 45
    path = tmp_path / "nested" / "config.json"
    cfg.save(path)
    assert path.exists()

    loaded = Config.load(path)
    assert loaded.portal.candidates[0].host == "10.0.0.1"
    assert loaded.portal.candidates[0].eportal_port == 8081
    assert loaded.account.carrier == "校园电信"
    assert loaded.monitor.check_interval_seconds == 45
    assert loaded.account.password == ""  # 明文不落盘


def test_load_missing_returns_defaults(tmp_path):
    cfg = Config.load(tmp_path / "absent.json")
    assert cfg.portal.candidates[0].host == PORTAL_PLACEHOLDER


def test_invalid_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        Config.load(path)


def test_credentials_ready():
    cfg = Config()
    assert not cfg.credentials_ready()
    cfg.account.username = "u"
    assert not cfg.credentials_ready()
    cfg.account.password = "p"
    assert cfg.credentials_ready()
