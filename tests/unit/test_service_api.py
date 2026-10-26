import pytest

from netanchor.config import Config, PortalCandidate
from netanchor.config_store import ConfigStore
from netanchor.service_api import RuntimeStatus, ServiceApi, masked_config


@pytest.fixture()
def store(tmp_path):
    store = ConfigStore(tmp_path / "nac", apply_acl=False)
    config = Config()
    config.portal.candidates = [PortalCandidate(name="t", host="10.0.0.1")]
    store.save(config)
    return store


def make_api(store, admin=False, actions=None):
    return ServiceApi(store, RuntimeStatus(state="online"), actions=actions or {})


def test_status_available_to_non_admin(store):
    api = make_api(store)
    response = api.handle({"id": "1", "cmd": "status"}, is_admin=False)
    assert response["ok"] is True
    assert response["data"]["state"] == "online"
    assert "uptime_seconds" in response["data"]


def test_write_commands_allowed_for_interactive_user(store):
    """写操作不再要求管理员；安全边界靠凭据与地址绑定。"""
    api = make_api(store)
    response = api.handle(
        {"id": "1", "cmd": "creds.set",
         "data": {"username": "20240001", "password": "p", "carrier": "校园其他"}},
        is_admin=False,
    )
    assert response["ok"] is True
    assert store.load().account.username == "20240001"


def test_portal_change_requires_password(store):
    store.set_credentials("20240001", "p@ss", "校园其他")
    api = make_api(store)
    payload = store.load(decrypt=False).to_dict()
    payload["portal"]["candidates"][0]["host"] = "10.0.0.2"

    response = api.handle({"id": "1", "cmd": "config.set", "data": {"config": payload}}, is_admin=False)
    assert response["ok"] is False
    assert response["error"]["code"] == "reauth_required"


def test_portal_change_with_password_rebinds_ciphertext(store):
    store.set_credentials("20240001", "p@ss", "校园其他")
    old_cipher = store.load(decrypt=False).account.password_protected

    api = make_api(store)
    payload = store.load(decrypt=False).to_dict()
    payload["portal"]["candidates"][0]["host"] = "10.0.0.2"

    response = api.handle(
        {"id": "1", "cmd": "config.set", "data": {"config": payload, "password": "p@ss"}},
        is_admin=False,
    )
    assert response["ok"] is True
    assert response["data"]["portal_changed"] is True

    reloaded = store.load()
    assert reloaded.account.password == "p@ss"
    assert reloaded.account.password_context == reloaded.portal_fingerprint
    assert reloaded.account.password_protected != old_cipher


def test_unknown_command(store):
    api = make_api(store)
    response = api.handle({"id": "1", "cmd": "nope"}, is_admin=True)
    assert response["error"]["code"] == "unknown_command"


def test_missing_cmd(store):
    api = make_api(store)
    response = api.handle({"id": "1"}, is_admin=True)
    assert response["error"]["code"] == "invalid_request"


def test_config_get_masks_secret(store):
    store.set_credentials("20240001", "p@ss", "校园电信")
    api = make_api(store)
    response = api.handle({"id": "1", "cmd": "config.get"}, is_admin=False)
    account = response["data"]["config"]["account"]
    assert "password" not in account
    assert account["password_protected"] == "***"
    assert account["username"] == "20240001"


def test_config_set_preserves_stored_password(store):
    store.set_credentials("20240001", "p@ss", "校园电信")
    api = make_api(store)
    current = store.load(decrypt=False)

    payload = current.to_dict()
    payload["monitor"]["check_interval_seconds"] = 12
    response = api.handle(
        {"id": "1", "cmd": "config.set", "data": {"config": payload}}, is_admin=True
    )
    assert response["ok"] is True

    reloaded = store.load()
    assert reloaded.monitor.check_interval_seconds == 12
    assert reloaded.account.password == "p@ss"  # 密文未被冲掉


def test_config_set_rejects_placeholder_host(store):
    api = make_api(store)
    payload = Config().to_dict()  # 占位 host
    response = api.handle(
        {"id": "1", "cmd": "config.set", "data": {"config": payload}}, is_admin=True
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_config"


def test_creds_set_validates(store):
    api = make_api(store)
    for data in (
        {"username": "", "password": "x", "carrier": "校园其他"},
        {"username": "u", "password": "", "carrier": "校园其他"},
        {"username": "u", "password": "p", "carrier": "不存在"},
    ):
        response = api.handle({"id": "1", "cmd": "creds.set", "data": data}, is_admin=True)
        assert response["ok"] is False
        assert response["error"]["code"] == "invalid_config"


def test_actions_dispatch(store):
    calls = []

    def action():
        calls.append(1)
        return "ok"

    api = make_api(store, actions={"monitor.start": action})
    response = api.handle({"id": "1", "cmd": "monitor.start"}, is_admin=True)
    assert response["ok"] is True
    assert response["data"]["result"] == "ok"
    assert calls == [1]


def test_masked_config_keeps_ciphertext_marker(store):
    store.set_credentials("u", "p", "校园其他")
    masked = masked_config(store.load())
    assert masked["account"]["password_protected"] == "***"
