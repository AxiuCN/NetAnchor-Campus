"""Named Pipe IPC：长度前缀 JSON，DACL 受限，客户端校验服务端身份。

安全模型：
- 管道 DACL：SYSTEM / Administrators 完全控制；INTERACTIVE 只有读写数据与同步，
  **不含 FILE_CREATE_PIPE_INSTANCE**（否则普通用户可再造一个同名实例冒充服务端）
- 服务端首实例带 FILE_FLAG_FIRST_PIPE_INSTANCE：名字被抢占时启动即失败，而不是静默共存
- 客户端在发送任何数据前校验对端：它必须是 SCM 里登记为 LocalSystem、
  影像为 NetAnchor.Service.exe 的那个服务进程。核对走 SCM 而不是读对端令牌，
  因为普通用户读不到 SYSTEM 进程的令牌（OpenProcess 直接 ERROR_ACCESS_DENIED）
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import struct
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

import pywintypes
import win32file
import win32pipe
import win32security
import win32service

from .constants import PIPE_NAME, SERVICE_NAME

logger = logging.getLogger(__name__)

# GENERIC_READ|GENERIC_WRITE 去掉 FILE_CREATE_PIPE_INSTANCE(0x4)
# 0x100083 = FILE_READ_DATA | FILE_WRITE_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE
PIPE_SDDL = "D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x100083;;;IU)"
FILE_READ_DATA = 0x0001
FILE_WRITE_DATA = 0x0002
FILE_READ_ATTRIBUTES = 0x0080
SYNCHRONIZE = 0x00100000
PIPE_ACCESS_CLIENT = FILE_READ_DATA | FILE_WRITE_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
# 注：PIPE_REJECT_REMOTE_CLIENTS(0x8) 在本机 Windows 10 21H2 上被 CreateNamedPipe
# 以 ERROR_INVALID_PARAMETER 拒绝，故未使用；远程访问由 DACL 排除 NETWORK/ANONYMOUS 主体。

MAX_FRAME = 64 * 1024
PROTOCOL_VERSION = 1

SYSTEM_ACCOUNTS = {"localsystem", "nt authority\\system", "system"}
DEFAULT_SERVER_IMAGE = "NetAnchor.Service.exe"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

kernel32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL


class IpcError(Exception):
    """IPC 层错误：连接失败、帧非法、超时、对端身份不可信。"""


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


# ---------------- 身份检查 ----------------

def is_admin_token(pid: int) -> bool:
    """检查进程令牌是否为提权的 Administrators。被 UAC 过滤的令牌不算。"""
    if not pid:
        return False
    try:
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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


def server_pid(handle) -> int:
    pid = wintypes.ULONG()
    if kernel32.GetNamedPipeServerProcessId(int(handle), ctypes.byref(pid)):
        return int(pid.value)
    return 0


def registered_service(service_name: str = SERVICE_NAME) -> tuple[int, str, str] | None:
    """向 SCM 查询服务登记的（进程 ID、运行账户、影像路径）。

    普通用户有权查询服务状态与配置（改配置才需要管理员），所以客户端不必提权。
    服务未安装、查询被拒或查询失败一律返回 None。
    """
    try:
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    except win32service.error:
        return None
    try:
        try:
            service = win32service.OpenService(
                scm,
                service_name,
                win32service.SERVICE_QUERY_STATUS | win32service.SERVICE_QUERY_CONFIG,
            )
        except win32service.error:
            return None
        try:
            # pywin32 的 QueryServiceStatusEx 只接受句柄，返回含 ProcessId 的字典；
            # QueryServiceConfig 返回位置元组：(类型, 启动类型, 错误控制, BinaryPathName,
            # LoadOrderGroup, TagId, Dependencies, ServiceStartName, DisplayName)
            status = win32service.QueryServiceStatusEx(service)
            config = win32service.QueryServiceConfig(service)
            process_id = int(status["ProcessId"])
            account = str(config[7])
            binary_path = str(config[3])
        finally:
            win32service.CloseServiceHandle(service)
        return process_id, account, binary_path
    except win32service.error:
        return None
    finally:
        win32service.CloseServiceHandle(scm)


def identity_problem(
    pid: int,
    registered: tuple[int, str, str] | None,
    expected_image: str = DEFAULT_SERVER_IMAGE,
    service_name: str = SERVICE_NAME,
) -> str:
    """纯判定：对端 PID 与 SCM 登记信息是否自洽。空串表示通过，否则返回原因。

    比对三项：登记的进程就是管道对端、账户是 LocalSystem、影像是预期文件。
    能改这三项的只有管理员，因此与"直接读对端令牌"等价。
    """
    if not pid:
        return "无法取得管道对端的进程 ID"
    if registered is None:
        return f"无法读取服务 {service_name} 的登记信息（未安装或查询被拒）"
    service_pid, account, binary_path = registered
    if service_pid != pid:
        return f"管道对端 PID {pid} 与服务登记的进程 {service_pid} 不一致"
    if account.strip().lower() not in SYSTEM_ACCOUNTS:
        return f"服务登记的运行账户为 {account}，不是 LocalSystem"
    image = Path(binary_path.strip().strip('"')).name
    if image.lower() != expected_image.lower():
        return f"服务登记的影像为 {image}，不是 {expected_image}"
    return ""


def check_server_identity(
    handle,
    expected_image: str = DEFAULT_SERVER_IMAGE,
    service_name: str = SERVICE_NAME,
) -> str:
    """发送任何请求之前校验对端；空串表示通过，否则返回拒绝原因。"""
    return identity_problem(
        server_pid(handle), registered_service(service_name), expected_image, service_name
    )


def verify_server_identity(handle, expected_image: str = DEFAULT_SERVER_IMAGE) -> bool:
    """`check_server_identity` 的布尔包装。"""
    return check_server_identity(handle, expected_image) == ""


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
        self._ready = threading.Event()
        self.last_error = ""

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

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

    def _create_pipe(self):
        """创建实例；首实例语义，被冒充实例占用时重试后放弃并记录错误。"""
        for _ in range(10):
            if self._stop.is_set():
                return None
            try:
                return win32pipe.CreateNamedPipe(
                    self._pipe_name,
                    win32pipe.PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                    8, MAX_FRAME, MAX_FRAME, 0, self._security(),
                )
            except pywintypes.error as exc:
                if exc.winerror in (5, 231):  # ACCESS_DENIED / PIPE_BUSY
                    logger.warning("创建管道实例失败，可能被其它实例占用: winerror=%s，重试", exc.winerror)
                    time.sleep(0.2)
                    continue
                self.last_error = f"创建管道失败: {exc}"
                logger.error(self.last_error)
                return None
        self.last_error = "管道名持续被占用，服务端放弃创建实例"
        logger.error(self.last_error)
        return None

    def run(self) -> None:
        # 线程里未捕获的异常会静默终止，必须自己记下来
        try:
            self._run()
        except BaseException:  # noqa: BLE001
            self.last_error = self.last_error or "IPC 服务端线程异常退出"
            logger.exception("IPC 服务端线程异常退出")

    def _run(self) -> None:
        announced = False
        while not self._stop.is_set():
            handle = self._create_pipe()
            if handle is None:
                return
            if not announced:
                announced = True
                self._ready.set()
                logger.info("IPC 管道已就绪: %s", self._pipe_name)

            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "winerror", None) != 535:  # ERROR_PIPE_CONNECTED
                    handle.Close()
                    continue
            try:
                self._serve_once(handle)
            except Exception:  # noqa: BLE001
                # 单个连接出问题（含 pywintypes.error 类身份不一致的情况）不能终止服务端
                logger.warning("处理 IPC 连接时异常，继续提供服务", exc_info=True)
            finally:
                try:
                    win32pipe.DisconnectNamedPipe(handle)
                except pywintypes.error:
                    pass
                handle.Close()

    def _serve_once(self, handle) -> None:
        try:
            _err, data = win32file.ReadFile(handle, MAX_FRAME)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "winerror", None) in (109, 233):  # BROKEN_PIPE / PIPE_NOT_CONNECTED
                return
            raise
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
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "winerror", None) not in (109, 233):
                logger.debug("回写响应失败", exc_info=True)


def _error(request_id: str, code: str, message: str) -> dict:
    return {"v": PROTOCOL_VERSION, "id": request_id, "ok": False,
            "error": {"code": code, "message": message}}


# ---------------- 客户端 ----------------

def service_state() -> str | None:
    """只读查询服务状态，用于把"管道不存在"翻译成人话。"""
    try:
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    except win32service.error:
        return None
    try:
        try:
            service = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
        except win32service.error:
            return None
        try:
            status = win32service.QueryServiceStatus(service)[1]
        finally:
            win32service.CloseServiceHandle(service)
        return {
            win32service.SERVICE_STOPPED: "stopped",
            win32service.SERVICE_RUNNING: "running",
            win32service.SERVICE_START_PENDING: "start_pending",
            win32service.SERVICE_STOP_PENDING: "stop_pending",
        }.get(status, str(status))
    finally:
        win32service.CloseServiceHandle(scm)


def _connect_hint(winerror: int) -> str:
    if winerror != 2:  # ERROR_FILE_NOT_FOUND：管道不存在
        return ""
    state = service_state()
    if state is None:
        return "服务未安装"
    if state != "running":
        return f"服务状态为 {state}，未在运行"
    return "服务在运行但未创建 IPC 管道，请查看服务日志（服务可能是启动失败或被占用）"


class PipeClient:
    def __init__(
        self,
        pipe_name: str = PIPE_NAME,
        timeout_ms: int = 5000,
        allow_insecure: bool = False,
        expected_image: str = DEFAULT_SERVER_IMAGE,
    ) -> None:
        self._pipe_name = pipe_name
        self._timeout_ms = timeout_ms
        # 开发态（--console 下服务是 python.exe、非 SYSTEM）可用 NA_IPC_INSECURE=1 放行
        self._allow_insecure = allow_insecure or os.environ.get("NA_IPC_INSECURE") == "1"
        self._expected_image = expected_image

    def _connect(self):
        deadline = time.monotonic() + self._timeout_ms / 1000
        retry_missing: bool | None = None
        while True:
            try:
                return win32file.CreateFile(
                    self._pipe_name, PIPE_ACCESS_CLIENT, 0, None,
                    win32file.OPEN_EXISTING, 0, None,
                )
            except pywintypes.error as exc:
                retryable = False
                if exc.winerror == 231:  # ERROR_PIPE_BUSY：上一个连接还没断开
                    retryable = True
                elif exc.winerror == 2:  # ERROR_FILE_NOT_FOUND
                    # 服务端只挂一个实例，处理完一个请求到建下一个实例之间有毫秒级空窗，
                    # 撞上就表现为"管道不存在"。只有服务确实在跑才值得等，否则快速报错。
                    if retry_missing is None:
                        retry_missing = service_state() == "running"
                    retryable = retry_missing
                if retryable and time.monotonic() < deadline:
                    time.sleep(0.05)
                    continue
                hint = _connect_hint(exc.winerror)
                detail = f"（{hint}）" if hint else ""
                raise IpcError(f"连接服务失败{detail}: {exc.strerror or exc}") from exc

    def request(self, cmd: str, data: dict | None = None, request_id: str = "cli") -> dict:
        payload = encode_frame({"v": PROTOCOL_VERSION, "id": request_id, "cmd": cmd, "data": data or {}})
        handle = self._connect()
        try:
            if not self._allow_insecure:
                problem = check_server_identity(handle, self._expected_image)
                if problem:
                    raise IpcError(f"无法确认服务端身份（{problem}），已拒绝发送请求")
            win32file.WriteFile(handle, payload)
            _err, raw = win32file.ReadFile(handle, MAX_FRAME)
        except pywintypes.error as exc:
            raise IpcError(f"与服务通信失败: {exc.strerror or exc}") from exc
        finally:
            handle.Close()
        return decode_frame(raw)
