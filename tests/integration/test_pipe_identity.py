"""客户端必须能识别"冒充的服务端实例"。

测试里的服务端由当前用户进程创建（非 SYSTEM），因此真实客户端应当拒绝，
并且拒绝发生在发送任何数据之前。
"""
import ctypes
import os
import time
import uuid

import pytest
import win32file

from netanchor.ipc import (
    DEFAULT_SERVER_IMAGE,
    PIPE_ACCESS_CLIENT,
    PIPE_SDDL,
    IpcError,
    PipeClient,
    PipeServer,
    verify_server_identity,
)


@pytest.fixture()
def pipe_name():
    return "\\\\.\\pipe\\NetAnchorIdTest-" + uuid.uuid4().hex


def _ok_response(request: dict) -> dict:
    return {"v": 1, "id": str(request.get("id", "?")), "ok": True, "data": {"state": "online"}}


def _start_rogue(pipe_name, calls):
    def handler(request, _is_admin):
        calls.append(request)
        return _ok_response(request)

    server = PipeServer(handler, pipe_name=pipe_name)
    server.start()
    time.sleep(0.3)
    return server


def _request_with_connect_retry(client: PipeClient, cmd: str = "status"):
    last_error = None
    for _ in range(40):
        try:
            return client.request(cmd)
        except IpcError as exc:
            if "无法确认服务端身份" in str(exc):
                raise
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"始终连不上测试管道: {last_error}")


def test_sddl_does_not_grant_instance_creation():
    assert "0x100083" in PIPE_SDDL
    assert "GRGW" not in PIPE_SDDL
    # 不能给 INTERACTIVE 完全控制（那会包含创建实例权限）
    assert "GA;;;IU" not in PIPE_SDDL


def test_client_rejects_untrusted_server(pipe_name):
    calls = []
    server = _start_rogue(pipe_name, calls)
    try:
        client = PipeClient(pipe_name, timeout_ms=3000)
        with pytest.raises(IpcError, match="无法确认服务端身份"):
            _request_with_connect_retry(client)
        time.sleep(0.2)
        assert calls == [], "被拒绝的连接不应向冒充实例发送任何请求"
    finally:
        server.stop()
        server.join(timeout=3)


def test_client_rejects_wrong_expected_image(pipe_name):
    calls = []
    server = _start_rogue(pipe_name, calls)
    try:
        client = PipeClient(pipe_name, timeout_ms=3000, expected_image="Other.Service.exe")
        with pytest.raises(IpcError, match="无法确认服务端身份"):
            _request_with_connect_retry(client)
        assert calls == []
    finally:
        server.stop()
        server.join(timeout=3)


def test_insecure_mode_still_reaches_server(pipe_name):
    """对照组：放行后应正常往返，证明拒绝原因是身份而不是链路。"""
    calls = []
    server = _start_rogue(pipe_name, calls)
    try:
        client = PipeClient(pipe_name, timeout_ms=3000, allow_insecure=True)
        response = _request_with_connect_retry(client)
        assert response["ok"] is True
        assert calls and calls[0]["cmd"] == "status"
    finally:
        server.stop()
        server.join(timeout=3)


def test_verify_server_identity_false_for_non_system(pipe_name):
    calls = []
    server = _start_rogue(pipe_name, calls)
    try:
        handle = win32file.CreateFile(
            pipe_name, PIPE_ACCESS_CLIENT, 0, None, win32file.OPEN_EXISTING, 0, None
        )
        try:
            assert verify_server_identity(handle, DEFAULT_SERVER_IMAGE) is False
        finally:
            handle.Close()
    finally:
        server.stop()
        server.join(timeout=3)


@pytest.mark.skipif(
    bool(ctypes.windll.shell32.IsUserAnAdmin()),
    reason="管理员令牌不受 IU 权限限制，无法验证创建实例被拒",
)
def test_second_instance_denied_for_interactive_user(pipe_name):
    calls = []
    server = _start_rogue(pipe_name, calls)
    try:
        import win32pipe

        with pytest.raises(Exception):
            win32pipe.CreateNamedPipe(
                pipe_name,
                0x00000003,  # PIPE_ACCESS_DUPLEX
                0x00000006,  # PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE
                8, 4096, 4096, 0, None,
            )
    finally:
        server.stop()
        server.join(timeout=3)
