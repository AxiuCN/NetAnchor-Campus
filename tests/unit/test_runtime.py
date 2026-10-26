import threading
import time
import uuid

import pytest

from netanchor.config import Config, PortalCandidate
from netanchor.config_store import ConfigStore
from netanchor.ipc import IpcError, PipeClient
from netanchor.portal import LoginResult
from netanchor.runtime import ServiceRuntime


class FakePortal:
    def __init__(self):
        self.calls = []

    def pick_candidate(self, candidates, timeout=None):
        self.calls.append("probe")
        return candidates[0]

    def fetch_captcha(self, candidate):
        self.calls.append("captcha")
        return b"img"

    def check_captcha(self, candidate, text):
        self.calls.append("check")
        return True

    def login(self, candidate, username, password):
        self.calls.append("login")
        return LoginResult(True, "1", "")


@pytest.fixture()
def store(tmp_path):
    store = ConfigStore(tmp_path / "nac")
    config = Config()
    config.portal.candidates = [PortalCandidate(name="t", host="10.0.0.1")]
    config.account.username = "20240001"
    config.account.password = "secret"
    config.monitor.check_interval_seconds = 1
    store.save(config)
    return store


def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def call(client: PipeClient, cmd: str, data=None, attempts: int = 30):
    last = None
    for _ in range(attempts):
        try:
            return client.request(cmd, data)
        except IpcError as exc:
            last = exc
            time.sleep(0.1)
    raise AssertionError(f"IPC 调用失败: {last}")


def make_runtime(store, online=True) -> ServiceRuntime:
    pipe = "\\\\.\\pipe\\NaRuntime-" + uuid.uuid4().hex
    return ServiceRuntime(
        store=store,
        portal=FakePortal(),
        recognize=lambda _img: "1234",
        check_online=lambda: online,
        pipe_name=pipe,
        console=False,
    )


def test_start_reaches_online_and_serves_ipc(store):
    runtime = make_runtime(store)
    runtime.start()
    try:
        client = PipeClient(runtime._pipe_name, timeout_ms=3000, allow_insecure=True)
        assert wait_until(lambda: runtime.status.state == "online")
        status = call(client, "status")["data"]
        assert status["state"] == "online"
        assert status["last_check_at"]
        config = call(client, "config.get")["data"]["config"]
        assert config["portal"]["candidates"][0]["host"] == "10.0.0.1"
    finally:
        runtime.stop()


def test_monitor_stop_and_start_via_ipc(store):
    runtime = make_runtime(store)
    runtime.start()
    try:
        client = PipeClient(runtime._pipe_name, timeout_ms=3000, allow_insecure=True)
        assert wait_until(lambda: runtime.status.state == "online")
        assert call(client, "monitor.stop")["ok"] is True
        assert runtime.status.state == "stopped"
        assert call(client, "monitor.start")["ok"] is True
        assert wait_until(lambda: runtime.status.state == "online")
    finally:
        runtime.stop()


def test_stop_releases_pipe(store):
    runtime = make_runtime(store)
    runtime.start()
    client = PipeClient(runtime._pipe_name, timeout_ms=2000, allow_insecure=True)
    assert wait_until(lambda: runtime.status.state == "online")
    runtime.stop()
    with pytest.raises(IpcError):
        client.request("status")


def test_offline_path_attempts_login(store):
    runtime = make_runtime(store, online=False)
    runtime.start()
    try:
        assert wait_until(lambda: runtime.status.state in {"reconnecting", "portal_unreachable"}, timeout=15)
        assert runtime.status.consecutive_failures >= 1
        assert runtime.status.last_error
    finally:
        runtime.stop()


def test_reload_applies_new_interval(store):
    runtime = make_runtime(store)
    runtime.start()
    try:
        config = store.load()
        config.monitor.check_interval_seconds = 7
        store.save(config)
        runtime.reload()
        assert runtime.config.monitor.check_interval_seconds == 7
    finally:
        runtime.stop()


def test_stop_with_blocking_ocr_keeps_thread_and_blocks_restart(store):
    """#2：停止超时后不得丢弃活线程，也不得再起第二个认证线程。"""
    entered = threading.Event()
    release = threading.Event()
    online = {"value": False}

    def recognize(_image):
        entered.set()
        release.wait(10)
        return "1234"

    pipe = "\\\\.\\pipe\\NaRuntime-" + uuid.uuid4().hex
    runtime = ServiceRuntime(
        store=store,
        portal=FakePortal(),
        recognize=recognize,
        check_online=lambda: online["value"],
        pipe_name=pipe,
        stop_grace_seconds=2.0,
    )
    runtime.start()
    try:
        assert entered.wait(5), "识别器没有被调用"
        first_thread = runtime._monitor_thread

        assert runtime._handle_stop() == "stopping"
        assert runtime._monitor_thread is first_thread
        assert first_thread.is_alive()

        # 旧线程未退出时，启动必须被拒绝，且不能替换线程对象
        assert runtime._handle_start() == "stopping"
        assert runtime._monitor_thread is first_thread

        release.set()
        assert wait_until(lambda: runtime._monitor_thread is None, timeout=10)
        assert runtime.status.state == "stopped"

        online["value"] = True
        assert runtime._handle_start() == "running"
        assert wait_until(lambda: runtime.status.state == "online", timeout=10)
    finally:
        release.set()
        runtime.stop()


def test_stop_when_idle_returns_stopped(store):
    runtime = make_runtime(store)
    runtime.start()
    try:
        assert wait_until(lambda: runtime.status.state == "online")
        assert runtime._handle_stop() == "stopped"
        assert runtime.status.state == "stopped"
    finally:
        runtime.stop()
