import time
import uuid

import pytest
import win32file

from netanchor.config import Config, PortalCandidate
from netanchor.config_store import ConfigStore
from netanchor.ipc import PIPE_ACCESS_CLIENT, IpcError, PipeClient, PipeServer
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


def test_server_reports_pipe_ready(tmp_path, pipe_name):
    store = make_store(tmp_path)
    server = start_server(store, pipe_name)
    try:
        assert server.wait_ready(3.0) is True
        assert server.ready is True
        assert server.last_error == ""
    finally:
        server.stop()
        server.join(timeout=3)


def test_client_explains_missing_service(monkeypatch):
    monkeypatch.setattr("netanchor.ipc.service_state", lambda: None)
    client = PipeClient("\\\\.\\pipe\\NaMissing-" + uuid.uuid4().hex, timeout_ms=300)
    with pytest.raises(IpcError, match="服务未安装"):
        client.request("status")


def test_server_survives_client_disconnect(tmp_path, pipe_name):
    """客户端连上就断（ERROR_BROKEN_PIPE）不能把服务端线程带走。"""
    store = make_store(tmp_path)
    server = start_server(store, pipe_name)
    try:
        assert server.wait_ready(3.0) is True
        handle = win32file.CreateFile(
            pipe_name, PIPE_ACCESS_CLIENT, 0, None, win32file.OPEN_EXISTING, 0, None
        )
        handle.Close()
        time.sleep(0.3)

        assert server.is_alive(), "一次异常断开不应终止服务端"
        client = PipeClient(pipe_name, timeout_ms=3000, allow_insecure=True)
        assert call_with_retry(client, "status")["ok"] is True
    finally:
        server.stop()
        server.join(timeout=3)


def test_client_retries_when_pipe_drops_midwrite(tmp_path, pipe_name):
    """服务端在断开与关闭句柄之间的窗口里会给出 233，客户端应重连重试而不是报错。"""
    import threading

    import pywintypes

    store = make_store(tmp_path)
    server = start_server(store, pipe_name)
    real_write = win32file.WriteFile
    counter = {"n": 0}
    main_thread = threading.current_thread()

    def flaky_write(handle, data):
        # 只干扰客户端（主线程）的前两次写，服务端响应照常
        if threading.current_thread() is main_thread and counter["n"] < 2:
            counter["n"] += 1
            raise pywintypes.error(233, "WriteFile", "管道的另一端上无任何进程。")
        return real_write(handle, data)

    try:
        assert server.wait_ready(3.0) is True
        client = PipeClient(pipe_name, timeout_ms=3000, allow_insecure=True)
        import netanchor.ipc as ipc_module

        original = ipc_module.win32file.WriteFile
        ipc_module.win32file.WriteFile = flaky_write
        try:
            response = client.request("status")
        finally:
            ipc_module.win32file.WriteFile = original
        assert response["ok"] is True
        assert counter["n"] == 2, "应当重试了两次"
    finally:
        server.stop()
        server.join(timeout=3)
