import threading
import time
import uuid

from netanchor.ui.tray import (
    TrayApp,
    acquire_single_instance,
    color_for_state,
    release_single_instance,
    status_text,
)


class _SlowClient:
    """每次调用都卡 2 秒，用来证明菜单回调不会阻塞消息循环。"""

    def __init__(self) -> None:
        self.called = threading.Event()

    def request(self, cmd, data=None):
        self.called.set()
        time.sleep(2)
        return {"ok": True, "data": {"result": "queued"}}


def test_menu_action_does_not_block_message_loop():
    client = _SlowClient()
    app = TrayApp(client=client)
    started = time.monotonic()
    app._on_reconnect()
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, f"菜单回调应立即返回，实际阻塞 {elapsed:.2f}s"
    assert client.called.wait(3), "后台线程最终应发出请求"


def test_color_for_known_states():
    assert color_for_state("online") == "#2ecc71"
    assert color_for_state("reconnecting") == "#e74c3c"
    assert color_for_state("stopped") == "#95a5a6"


def test_color_for_unknown_state_falls_back_to_grey():
    assert color_for_state("whatever") == "#95a5a6"


def test_status_text_online():
    assert status_text({"state": "online"}) == "状态：已连接"


def test_status_text_includes_error_when_offline():
    text = status_text({"state": "reconnecting", "last_error": "验证码校验未通过"})
    assert "重连中" in text
    assert "验证码校验未通过" in text


def test_status_text_without_error():
    assert status_text({"state": "stopped"}) == "状态：已停止"


def test_status_text_unknown_state_uses_raw_value():
    assert "weird" in status_text({"state": "weird"})


def test_single_instance_mutex():
    name = "Local\\NaTrayTest-" + uuid.uuid4().hex
    try:
        assert acquire_single_instance(name) is True
        assert acquire_single_instance(name) is False
    finally:
        release_single_instance()
