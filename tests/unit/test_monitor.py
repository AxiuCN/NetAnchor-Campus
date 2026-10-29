import threading

import pytest

from netanchor.config import Config, PortalCandidate
from netanchor.monitor import Monitor, State
from netanchor.portal import LoginResult


class FakePortal:
    def __init__(self, candidate=None, captcha=b"img", check_ok=True, login=LoginResult(True, "1", "")):
        self.candidate = candidate or PortalCandidate(name="fake", host="10.0.0.1")
        self.captcha = captcha
        self.check_ok = check_ok
        self.login_result = login
        self.probe_calls = 0
        self.fetch_calls = 0
        self.check_calls = 0
        self.login_calls = 0
        self.checked_texts = []

    def pick_candidate(self, candidates, timeout=None):
        self.probe_calls += 1
        return self.candidate

    def fetch_captcha(self, candidate):
        self.fetch_calls += 1
        return self.captcha

    def check_captcha(self, candidate, text):
        self.check_calls += 1
        self.checked_texts.append(text)
        return self.check_ok

    def login(self, candidate, username, password):
        self.login_calls += 1
        self.credentials = (username, password)
        return self.login_result


def make_config(**overrides) -> Config:
    cfg = Config()
    cfg.portal.candidates = [PortalCandidate(name="fake", host="10.0.0.1")]
    cfg.account.username = "20240001"
    cfg.account.password = "secret"
    cfg.monitor.max_captcha_attempts = 3
    for key, value in overrides.items():
        setattr(cfg.monitor, key, value)
    return cfg


def make_monitor(portal, online_sequence, recognitions):
    online_iter = iter(online_sequence)
    recogn_iter = iter(recognitions)
    sleeps = []

    def check_online():
        return next(online_iter)

    def recognize(_image):
        return next(recogn_iter)

    monitor = Monitor(
        config=make_config(),
        portal=portal,
        recognize=recognize,
        check_online=check_online,
        sleep=sleeps.append,
    )
    return monitor, sleeps


def test_online_short_circuits():
    portal = FakePortal()
    monitor, _ = make_monitor(portal, [True], [])
    result = monitor.cycle()
    assert result.state is State.ONLINE
    assert portal.probe_calls == 0
    assert portal.fetch_calls == 0


def test_missing_credentials():
    portal = FakePortal()
    monitor, _ = make_monitor(portal, [False], [])
    monitor._config.account.password = ""
    result = monitor.cycle()
    assert result.state is State.CONFIG_MISSING
    assert portal.fetch_calls == 0


def test_portal_unreachable():
    portal = FakePortal()
    portal.pick_candidate = lambda candidates, timeout=None: None
    monitor, _ = make_monitor(portal, [False], [])
    result = monitor.cycle()
    assert result.state is State.PORTAL_UNREACHABLE
    assert result.consecutive_failures == 1


def test_portal_unreachable_carries_probe_reason():
    """状态里要带出探测失败的具体原因，否则"不可达"会被误读成断网。"""
    portal = FakePortal()
    portal.pick_candidate = lambda candidates, timeout=None: None
    portal.last_probe_error = "getaddrinfo failed"
    monitor, _ = make_monitor(portal, [False], [])
    result = monitor.cycle()
    assert result.state is State.PORTAL_UNREACHABLE
    assert "getaddrinfo failed" in result.detail


def test_captcha_retries_up_to_limit():
    portal = FakePortal(check_ok=False)
    monitor, _ = make_monitor(portal, [False], ["1111", "2222", "3333"])
    result = monitor.cycle()
    assert result.state is State.RECONNECTING
    assert portal.fetch_calls == 3
    assert portal.check_calls == 3
    assert portal.login_calls == 0


def test_bad_length_does_not_consume_validation():
    portal = FakePortal(check_ok=True)
    monitor, sleeps = make_monitor(portal, [False, True], ["123", "1234"])
    result = monitor.cycle()
    assert result.state is State.ONLINE
    assert portal.fetch_calls == 2
    assert portal.check_calls == 1
    assert portal.login_calls == 1
    assert sleeps == [2]  # 登录后复检前的等待


def test_login_failure_reports_message():
    portal = FakePortal(login=LoginResult(False, "0", "密码错误"))
    monitor, _ = make_monitor(portal, [False], ["1234"])
    result = monitor.cycle()
    assert result.state is State.RECONNECTING
    assert "密码错误" in result.detail
    assert result.consecutive_failures == 1


def test_login_success_but_still_offline():
    portal = FakePortal()
    monitor, _ = make_monitor(portal, [False, False], ["1234"])
    result = monitor.cycle()
    assert result.state is State.RECONNECTING
    assert "复检" in result.detail


def test_backoff_ladder_and_cap():
    monitor, _ = make_monitor(FakePortal(), [True], [])
    ladder = make_config().monitor.backoff_seconds
    for index, expected in enumerate(ladder, start=1):
        assert monitor.compute_backoff(index) == expected
    assert monitor.compute_backoff(99) == ladder[-1]


def test_run_loop_emits_states_and_stops():
    portal = FakePortal()
    stop = threading.Event()
    seen = []

    def on_state(result):
        seen.append(result.state)
        stop.set()  # 一轮后停止

    monitor = Monitor(
        config=make_config(),
        portal=portal,
        recognize=lambda _img: "1234",
        check_online=lambda: True,
        sleep=lambda _s: None,
        on_state=on_state,
        stop_event=stop,
    )
    monitor.run()
    assert seen[0] is State.ONLINE
    # 循环结束时补发 STOPPED
    assert seen[-1] is State.STOPPED


def test_cancelled_before_cycle_returns_stopped():
    stop = threading.Event()
    stop.set()
    portal = FakePortal()
    monitor = Monitor(make_config(), portal, lambda _img: "1234", lambda: False, stop_event=stop)
    result = monitor.cycle()
    assert result.state is State.STOPPED
    assert portal.probe_calls == 0


def test_cancel_during_validation_skips_login():
    stop = threading.Event()
    portal = FakePortal(check_ok=True)

    def check_then_cancel(candidate, text):
        portal.check_calls += 1
        stop.set()
        return True

    portal.check_captcha = check_then_cancel
    monitor = Monitor(make_config(), portal, lambda _img: "1234", lambda: False, stop_event=stop)
    result = monitor.cycle()
    assert result.state is State.STOPPED
    assert portal.login_calls == 0


def test_cipher_bound_to_other_portal_returns_config_missing():
    """#3：地址变更后旧密文不可用，不能进入探测/取码/登录。"""
    config = make_config()
    config.account.password = ""
    config.account.password_protected = "cipher-for-old-portal"
    config.account.password_context = "10.0.0.9|80|801"
    assert config.credentials_ready() is False

    portal = FakePortal()
    monitor = Monitor(config, portal, lambda _img: "1234", lambda: False)
    result = monitor.cycle()

    assert result.state is State.CONFIG_MISSING
    assert portal.probe_calls == 0
    assert portal.fetch_calls == 0
    assert portal.login_calls == 0


def test_empty_password_never_reaches_login():
    """即使 credentials_ready 为真（密文与地址匹配但明文缺失），也不得提交空密码。"""
    config = make_config()
    config.account.password = ""
    config.account.password_protected = "cipher"
    config.account.password_context = config.portal_fingerprint
    assert config.credentials_ready() is True

    portal = FakePortal()
    monitor = Monitor(config, portal, lambda _img: "1234", lambda: False)
    result = monitor.cycle()

    assert result.state is State.CONFIG_MISSING
    assert portal.fetch_calls == 0
    assert portal.check_calls == 0
    assert portal.login_calls == 0
