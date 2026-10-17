"""监控状态机：在线判定 → 门户探测 → 取码识别 → 校验 → 登录 → 复检。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .config import Config
from .errors import NetAnchorError, NetworkError, ProtocolError
from .portal import PortalClient

logger = logging.getLogger(__name__)


class State(str, Enum):
    ONLINE = "online"
    RECONNECTING = "reconnecting"
    PORTAL_UNREACHABLE = "portal_unreachable"
    CONFIG_MISSING = "config_missing"
    STOPPED = "stopped"


@dataclass(frozen=True)
class CycleResult:
    state: State
    detail: str = ""
    consecutive_failures: int = 0


class Monitor:
    """一次 `cycle()` 完成一轮判定；`run()` 负责间隔、退避与回调。

    依赖全部注入，便于离线测试：
    - `portal`：门户协议客户端
    - `recognize`：`bytes -> str`，把验证码图变成文本
    - `check_online`：`() -> bool`，连通性判定
    """

    def __init__(
        self,
        config: Config,
        portal: PortalClient,
        recognize: Callable[[bytes], str],
        check_online: Callable[[], bool],
        sleep: Callable[[float], None] = time.sleep,
        on_state: Callable[[CycleResult], None] | None = None,
    ) -> None:
        self._config = config
        self._portal = portal
        self._recognize = recognize
        self._check_online = check_online
        self._sleep = sleep
        self._on_state = on_state
        self._consecutive = 0
        self._last_detail = ""
        self._repeat = 0

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive

    # ---- 退避 ----
    def compute_backoff(self, consecutive: int | None = None) -> float:
        count = self._consecutive if consecutive is None else consecutive
        ladder = self._config.monitor.backoff_seconds
        if not ladder:
            return float(self._config.monitor.check_interval_seconds)
        index = min(max(count - 1, 0), len(ladder) - 1)
        return float(ladder[index])

    # ---- 单轮 ----
    def cycle(self) -> CycleResult:
        if self._check_online():
            self._mark_ok()
            return CycleResult(State.ONLINE, "", 0)

        account = self._config.account
        if not account.username or not (account.password or account.password_protected):
            return self._fail(State.CONFIG_MISSING, "缺少账号或密码")

        candidate = self._portal.pick_candidate(
            self._config.portal.candidates,
            timeout=self._config.portal.probe_timeout_seconds,
        )
        if candidate is None:
            return self._fail(State.PORTAL_UNREACHABLE, "所有门户候选均不可达")

        failure = self._authenticate(candidate)
        if failure is None:
            self._sleep(2)
            if self._check_online():
                self._mark_ok()
                return CycleResult(State.ONLINE, f"{candidate.host} 登录成功", 0)
            failure = "登录返回成功但连通性复检未通过"
        return self._fail(State.RECONNECTING, failure)

    def _authenticate(self, candidate) -> str | None:
        """返回 None 表示登录成功；否则返回失败原因。"""
        last_failure = "未进入登录流程"
        for attempt in range(1, self._config.monitor.max_captcha_attempts + 1):
            try:
                image = self._portal.fetch_captcha(candidate)
                text = self._recognize(image)
            except ProtocolError as exc:
                last_failure = f"取码/识别异常: {exc}"
                continue
            except NetworkError:
                raise

            if len(text) != 4:
                # 位数不对时直接换一张，不消耗服务器的校验机会
                last_failure = f"识别位数异常({text!r})，换码重试"
                continue

            if not self._portal.check_captcha(candidate, text):
                last_failure = "验证码校验未通过"
                continue

            result = self._portal.login(candidate, self._config.effective_username, self._config.account.password)
            if result.ok:
                return None
            # 登录被拒（密码错误、互踢、频控）时不再连续换码重试，交给外层退避
            return f"登录失败 result={result.code} {result.message}".strip()
        return last_failure

    # ---- 状态与日志 ----
    def _fail(self, state: State, detail: str) -> CycleResult:
        self._consecutive += 1
        if detail == self._last_detail:
            self._repeat += 1
            if self._repeat % 10 == 0:
                logger.warning("%s（连续第 %d 次）：%s", state.value, self._consecutive, detail)
        else:
            self._last_detail = detail
            self._repeat = 1
            logger.warning("%s：%s", state.value, detail)
        return CycleResult(state, detail, self._consecutive)

    def _mark_ok(self) -> None:
        if self._consecutive:
            logger.info("已恢复在线（此前连续失败 %d 次）", self._consecutive)
        self._consecutive = 0
        self._last_detail = ""
        self._repeat = 0

    # ---- 常驻循环 ----
    def run(self, stop_event, wake_event=None) -> None:
        while not stop_event.is_set():
            try:
                result = self.cycle()
            except NetworkError as exc:
                result = self._fail(State.RECONNECTING, f"网络异常: {exc}")
            except ProtocolError as exc:
                result = self._fail(State.RECONNECTING, f"协议异常: {exc}")
            except NetAnchorError as exc:
                result = self._fail(State.RECONNECTING, f"异常: {exc}")
            except Exception:  # 兜底：不能让监控线程死掉
                logger.exception("监控循环未预期异常")
                result = self._fail(State.RECONNECTING, "未预期异常")

            if self._on_state:
                self._on_state(result)

            if stop_event.is_set():
                break

            if result.state is State.ONLINE:
                delay = float(self._config.monitor.check_interval_seconds)
            else:
                delay = self.compute_backoff()
            logger.debug("下一轮 %.0f 秒后（状态 %s）", delay, result.state.value)
            if wake_event is not None:
                # 等待期间可被「立即重连」或「停止」唤醒
                if wake_event.wait(delay):
                    wake_event.clear()
            else:
                stop_event.wait(delay)
        if self._on_state:
            self._on_state(CycleResult(State.STOPPED, "", self._consecutive))
