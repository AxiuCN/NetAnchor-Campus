import time
import uuid

import pytest

from netanchor.config import Config, PortalCandidate
from netanchor.config_store import ConfigStore
from netanchor.ipc import PipeClient, PipeServer
from netanchor.service_api import RuntimeStatus, ServiceApi


@pytest.fixture()
def pipe_name():
    return "\\\\.\\pipe\\NetAnchorTest-" + uuid.uuid4().hex


def make_store(tmp_path) -> ConfigStore:
    store = ConfigStore(tmp_path / "nac", apply_acl=False)
    config = Config()
    config.portal.candidates = [PortalCandidate(name="t", host="10.0.0.1")]
    store.save(config)
    return store


def start_server(store, pipe_name, admin=True):
    api = ServiceApi(store, RuntimeStatus(state="online"))
    server = PipeServer(api.handle, pipe_name=pipe_name, is_admin=lambda _pid: admin)
    server.start()
    return server


def call_with_retry(client: PipeClient, cmd: str, data=None, attempts: int = 20):
    last = None
    for _ in range(attempts):
        try:
            return client.request(cmd, data)
        except Exception as exc:  # 服务端可能还没进入 ConnectNamedPipe
            last = exc
            time.sleep(0.1)
    raise AssertionError(f"IPC 调用失败: {last}")


def test_status_roundtrip(tmp_path, pipe_name):
    store = make_store(tmp_path)
    server = start_server(store, pipe_name, admin=False)
    try:
        client = PipeClient(pipe_name, timeout_ms=3000, allow_insecure=True)
        response = call_with_retry(client, "status")
        assert response["ok"] is True
        assert response["data"]["state"] == "online"
    finally:
        server.stop()
        server.join(timeout=3)


def test_write_allowed_for_interactive_user(tmp_path, pipe_name):
    """非管理员也能写：安全边界改为凭据与门户地址绑定。"""
    store = make_store(tmp_path)
    server = start_server(store, pipe_name, admin=False)
    try:
        client = PipeClient(pipe_name, timeout_ms=3000, allow_insecure=True)
        response = call_with_retry(
            client, "creds.set",
            {"username": "20240001", "password": "p", "carrier": "校园其他"},
        )
        assert response["ok"] is True
        assert store.load().account.username == "20240001"
    finally:
        server.stop()
        server.join(timeout=3)


def test_write_allowed_for_admin(tmp_path, pipe_name):
    store = make_store(tmp_path)
    server = start_server(store, pipe_name, admin=True)
    try:
        client = PipeClient(pipe_name, timeout_ms=3000, allow_insecure=True)
        response = call_with_retry(
            client, "creds.set",
            {"username": "20240001", "password": "p", "carrier": "校园其他"},
        )
        assert response["ok"] is True
        assert store.load().account.username == "20240001"
    finally:
        server.stop()
        server.join(timeout=3)
