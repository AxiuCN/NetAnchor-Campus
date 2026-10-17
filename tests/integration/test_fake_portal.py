import os

import pytest

from netanchor.config import Config, PortalCandidate
from netanchor.errors import NetworkError, ProtocolError
from netanchor.http import HttpClient
from netanchor.monitor import Monitor, State
from netanchor.portal import PortalClient

from fake_portal import FakePortal


@pytest.fixture()
def portal():
    server = FakePortal()
    yield server
    server.stop()


def make_client(portal: FakePortal, timeout: float = 2.0):
    candidate = PortalCandidate(
        name="local", host="127.0.0.1",
        login_port=portal.login_port, eportal_port=portal.eportal_port,
    )
    return PortalClient(HttpClient(default_timeout=timeout), timeout=timeout), candidate


def make_config(portal: FakePortal) -> Config:
    cfg = Config()
    cfg.portal.candidates = [PortalCandidate(
        name="local", host="127.0.0.1",
        login_port=portal.login_port, eportal_port=portal.eportal_port,
    )]
    cfg.account.username = "20240001"
    cfg.account.password = "secret"
    return cfg


def test_probe_and_full_flow(portal):
    client, candidate = make_client(portal)
    assert client.probe(candidate) is True

    image = client.fetch_captcha(candidate)
    assert image.startswith(b"GIF89a")

    assert client.check_captcha(candidate, "1234") is True
    assert client.check_captcha(candidate, "0000") is False

    result = client.login(candidate, "20240001@dx", "secret")
    assert result.ok is True
    assert result.code == "1"


def test_probe_rejects_non_drcom_server():
    other = FakePortal()
    try:
        client, _ = make_client(other)
        # 把登录端口指到取码端口：该端口对 "/" 返回 404 且无 DrcomServer 头
        bad = PortalCandidate(host="127.0.0.1", login_port=other.eportal_port,
                              eportal_port=other.eportal_port)
        assert client.probe(bad) is False
    finally:
        other.stop()


def test_getcode_error_raises_protocol_error():
    server = FakePortal(getcode_status=500)
    try:
        client, candidate = make_client(server)
        with pytest.raises(ProtocolError):
            client.fetch_captcha(candidate)
    finally:
        server.stop()


def test_login_bad_json_raises_protocol_error():
    server = FakePortal(login_raw="<html>oops</html>")
    try:
        client, candidate = make_client(server)
        with pytest.raises(ProtocolError):
            client.login(candidate, "u", "p")
    finally:
        server.stop()


def test_check_captcha_timeout_raises_network_error():
    server = FakePortal(check_delay=1.0)
    try:
        client, candidate = make_client(server, timeout=0.3)
        with pytest.raises(NetworkError):
            client.check_captcha(candidate, "1234")
    finally:
        server.stop()


def test_requests_bypass_proxy_environment(portal, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    client, candidate = make_client(portal)
    assert client.probe(candidate) is True


def test_monitor_end_to_end_against_fake_portal(portal):
    client, _ = make_client(portal)
    config = make_config(portal)
    online = {"value": False}

    def check_online():
        return online["value"]

    calls = {"n": 0}

    def recognize(_image):
        calls["n"] += 1
        return "1234" if calls["n"] == 1 else "1234"

    def on_login_success_wait(_seconds):
        online["value"] = True  # 模拟登录后网络恢复

    monitor = Monitor(
        config=config,
        portal=client,
        recognize=recognize,
        check_online=check_online,
        sleep=on_login_success_wait,
    )
    result = monitor.cycle()
    assert result.state is State.ONLINE
    assert result.detail.endswith("登录成功")
    assert portal.check_calls == 1
