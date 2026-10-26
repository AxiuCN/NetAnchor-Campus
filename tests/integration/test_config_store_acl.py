"""配置存储与凭据加密。

设置 DACL 需要提权令牌：只有两个 ACL 用例需要管理员，其余用例在普通权限下也能跑
（`apply_acl=False`），这样外部审计用普通账户执行 pytest 不会得到一堆 PermissionError。
"""
import json

import pytest
import win32api
import win32security

from netanchor.config import Config, PortalCandidate
from netanchor.config_store import ConfigStore, describe_dacl
from netanchor.secrets import SecretError, protect, unprotect

SYSTEM_SID = win32security.ConvertSidToStringSid(
    win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
)
ADMIN_SID = win32security.ConvertSidToStringSid(
    win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
)


def _is_elevated() -> bool:
    try:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        return bool(win32security.GetTokenInformation(token, win32security.TokenElevation))
    except Exception:  # noqa: BLE001
        return False


requires_admin = pytest.mark.skipif(not _is_elevated(), reason="设置 ACL 需要提权令牌")


def make_store(tmp_path) -> ConfigStore:
    # 非 ACL 用例不碰 DACL，普通权限可跑
    return ConfigStore(tmp_path / "nac", apply_acl=False)


def valid_config() -> Config:
    config = Config()
    config.portal.candidates = [PortalCandidate(name="t", host="10.0.0.1")]
    return config


@requires_admin
def test_directory_dacl_only_system_and_admins(tmp_path):
    store = ConfigStore(tmp_path / "nac")  # 默认 apply_acl=True
    store.ensure_dir()
    sids = {sid for sid, _ in describe_dacl(store.base_dir)}
    assert sids == {SYSTEM_SID, ADMIN_SID}


@requires_admin
def test_file_dacl_only_system_and_admins(tmp_path):
    store = ConfigStore(tmp_path / "nac")
    path = store.save(valid_config())
    sids = {sid for sid, _ in describe_dacl(path)}
    assert sids == {SYSTEM_SID, ADMIN_SID}


def test_apply_acl_false_skips_dacl_calls(tmp_path, monkeypatch):
    import netanchor.config_store as module

    def boom(*_a, **_k):
        raise AssertionError("apply_acl=False 时不应设置 DACL")

    monkeypatch.setattr(module, "_apply_restricted_dacl", boom)
    store = ConfigStore(tmp_path / "nac", apply_acl=False)
    store.save(valid_config())
    assert store.path.exists()


def test_password_encrypted_on_disk_and_decrypts_on_load(tmp_path):
    store = make_store(tmp_path)
    store.ensure_dir()
    store.save(valid_config())
    store.set_credentials("20240001", "p@ss中文", "校园电信")

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert "password" not in raw["account"]
    assert raw["account"]["password_protected"] != "p@ss中文"

    loaded = store.load()
    assert loaded.account.password == "p@ss中文"
    assert loaded.account.username == "20240001"


def test_backup_created_on_overwrite(tmp_path):
    store = make_store(tmp_path)
    store.save(valid_config())
    store.save(valid_config())
    assert store.backup_path.exists()


def test_tampered_ciphertext_raises(tmp_path):
    store = make_store(tmp_path)
    store.ensure_dir()
    store.save(valid_config())
    store.set_credentials("u", "p", "校园其他")

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    cipher = raw["account"]["password_protected"]
    raw["account"]["password_protected"] = cipher[:-6] + ("AAAAAA" if not cipher.endswith("AAAAAA") else "BBBBBB")
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SecretError):
        store.load()


def test_secret_roundtrip_unicode_and_empty():
    assert unprotect(protect("密码 123")) == "密码 123"
    assert protect("") == ""
    assert unprotect("") == ""


def test_secret_context_binding():
    token = protect("p@ss", context="portal-a")
    assert unprotect(token, context="portal-a") == "p@ss"
    with pytest.raises(SecretError):
        unprotect(token, context="portal-b")


def test_changing_portal_invalidates_stored_password(tmp_path):
    store = make_store(tmp_path)
    store.ensure_dir()
    store.save(valid_config())
    store.set_credentials("20240001", "p@ss", "校园其他")
    assert store.load().account.password == "p@ss"

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["portal"]["candidates"][0]["host"] = "10.0.0.9"  # 手工改地址，模拟绕过 IPC
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    reloaded = store.load()
    assert reloaded.account.password == ""
    assert reloaded.credentials_ready() is False
