"""Named Pipe IPC：长度前缀 JSON，DACL 受限，写操作校验调用者权限。

安全模型：
- 管道 DACL：SYSTEM / Administrators 完全控制，INTERACTIVE 仅连接与读写
- 每个请求都解析客户端 PID 并检查其令牌是否属于 Administrators；
  是否放行由上层 `handler` 依据命令类型决定
"""
from __future__ import annotations

import ctypes
import json
import struct
import threading
from ctypes import wintypes
from typing import Callable

import pywintypes
import win32file
import win32pipe
import win32security

PIPE_NAME = r"\\.\pipe\NetAnchor.Campus.v1"
PIPE_SDDL = "D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;IU)"
MAX_FRAME = 64 * 1024
PROTOCOL_VERSION = 1

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class IpcError(Exception):
    """IPC 层错误：连接失败、帧非法、超时。"""


# ---------------- 帧编解码 ----------------

def encode_frame(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise IpcError(f"帧过大: {len(body)} 字节")
    return struct.pack("<I", len(body)) + body


def decode_frame(data: bytes) -> dict:
    if len(data) < 4:
        raise IpcError("帧长度不足 4 字节")
    (size,) = struct.unpack("<I", data[:4])
    if size > MAX_FRAME:
        raise IpcError(f"声明长度超限: {size}")
    body = data[4:4 + size]
    if len(body) != size:
        raise IpcError(f"帧被截断: 期望 {size}，实际 {len(body)}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IpcError("帧不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise IpcError("帧根节点必须是对象")
    return payload


# ---------------- 客户端权限 ----------------

def is_admin_token(pid: int) -> bool:
    """检查进程令牌是否为提权的 Administrators。被 UAC 过滤的令牌不算。"""
    if not pid:
        return False
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            token = win32security.OpenProcessToken(handle, win32security.TOKEN_QUERY)
            groups = win32security.GetTokenInformation(token, win32security.TokenGroups)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False

    admin_sid = win32security.ConvertSidToStringSid(
        win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
    )
    enabled = getattr(win32security, "SE_GROUP_ENABLED", 0x4)
    for sid, attributes in groups:
        if win32security.ConvertSidToStringSid(sid) == admin_sid:
            return bool(attributes & enabled)
    return False


def _client_pid(handle) -> int:
    pid = wintypes.ULONG()
    if kernel32.GetNamedPipeClientProcessId(int(handle), ctypes.byref(pid)):
        return int(pid.value)
    return 0


# ---------------- 服务端 ----------------

class PipeServer(threading.Thread):
    def __init__(
        self,
        handler: Callable[[dict, bool], dict],
        pipe_name: str = PIPE_NAME,
        sddl: str = PIPE_SDDL,
        is_admin: Callable[[int], bool] = is_admin_token,
    ) -> None:
        super().__init__(daemon=True, name="PipeServer")
        self._handler = handler
        self._pipe_name = pipe_name
        self._sddl = sddl
        self._is_admin = is_admin
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        try:  # 连接一次自身，解除 ConnectNamedPipe 的阻塞
            win32file.CreateFile(
                self._pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            ).Close()
        except pywintypes.error:
            pass

    def _security(self):
        descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            self._sddl, win32security.SDDL_REVISION_1
        )
        attributes = win32security.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = descriptor
        return attributes

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                handle = win32pipe.CreateNamedPipe(
                    self._pipe_name,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                    8, MAX_FRAME, MAX_FRAME, 0, self._security(),
                )
            except pywintypes.error:
                break

            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error as exc:
                if exc.winerror != 535:  # ERROR_PIPE_CONNECTED
                    handle.Close()
                    continue
            try:
                self._serve_once(handle)
            finally:
                try:
                    win32pipe.DisconnectNamedPipe(handle)
                except pywintypes.error:
                    pass
                handle.Close()

    def _serve_once(self, handle) -> None:
        try:
            _err, data = win32file.ReadFile(handle, MAX_FRAME)
        except pywintypes.error:
            return
        pid = _client_pid(handle)
        try:
            request = decode_frame(data)
        except IpcError as exc:
            response = _error(request_id="?", code="bad_frame", message=str(exc))
        else:
            try:
                response = self._handler(request, self._is_admin(pid))
            except Exception as exc:  # 处理器异常不能打断服务
                response = _error(str(request.get("id", "?")), "internal", f"{type(exc).__name__}: {exc}")
        try:
            win32file.WriteFile(handle, encode_frame(response))
        except pywintypes.error:
            pass


def _error(request_id: str, code: str, message: str) -> dict:
    return {"v": PROTOCOL_VERSION, "id": request_id, "ok": False,
            "error": {"code": code, "message": message}}


# ---------------- 客户端 ----------------

class PipeClient:
    def __init__(self, pipe_name: str = PIPE_NAME, timeout_ms: int = 5000) -> None:
        self._pipe_name = pipe_name
        self._timeout_ms = timeout_ms

    def request(self, cmd: str, data: dict | None = None, request_id: str = "cli") -> dict:
        payload = encode_frame({"v": PROTOCOL_VERSION, "id": request_id, "cmd": cmd, "data": data or {}})
        try:
            raw = win32pipe.CallNamedPipe(self._pipe_name, payload, MAX_FRAME, self._timeout_ms)
        except pywintypes.error as exc:
            raise IpcError(f"连接服务失败: {exc.strerror or exc}") from exc
        return decode_frame(raw)
