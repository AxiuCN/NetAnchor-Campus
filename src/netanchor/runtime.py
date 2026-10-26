"""服务运行时：把配置、日志、监控线程与 IPC 组装起来。

服务入口（SCM）与开发态 `--console` 都复用这个类，差别只在日志是否输出到控制台。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import __version__
from .config import Config
from .config_store import ConfigStore
from .errors import NetworkError
from .http import HttpClient
from .ipc import PIPE_NAME, PipeServer
from .logging_setup import get_logger, setup_logging
from .monitor import CycleResult, Monitor, State
from .ocr_client import SubprocessOcr, frozen_worker_command, is_frozen
from .portal import PortalClient
from .service_api import RuntimeStatus, ServiceApi


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _after(seconds: float) -> str:
    return (datetime.now() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


class ServiceRuntime:
    def __init__(
        self,
        store: ConfigStore | None = None,
        *,
        http: HttpClient | None = None,
        portal: PortalClient | None = None,
        recognize: Callable[[bytes], str] | None = None,
        check_online: Callable[[], bool] | None = None,
        console: bool = False,
        pipe_name: str = PIPE_NAME,
        version: str = __version__,
        stop_grace_seconds: float = 35.0,
    ) -> None:
        self._store = store or ConfigStore()
        self._http_override = http
        self._portal_override = portal
        self._recognize_override = recognize
        self._check_online_override = check_online
        self._console = console
        self._pipe_name = pipe_name
        self._version = version
        # OCR 子进程超时 30 s，停止/热加载要等它返回，否则会留下活线程
        self._stop_grace = stop_grace_seconds

        self.status = RuntimeStatus(state="starting")
        self._config = Config()
        self._logger = get_logger("runtime")

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._monitor: Monitor | None = None
        self._monitor_thread: threading.Thread | None = None
        self._server: PipeServer | None = None
        self._api: ServiceApi | None = None
        self._last_state: str = ""

    # ---- 生命周期 ----
    def start(self) -> None:
        self._logger = setup_logging(self.logs_dir, console=self._console)
        try:
            self._config = self._store.load()
        except Exception as exc:  # 配置坏了也要起得来，用默认配置并记录
            self._logger.error("加载配置失败，使用默认配置: %s", exc)
            self._config = Config()
        self._apply_log_level()

        self._api = ServiceApi(
            self._store,
            self.status,
            actions={
                "monitor.start": self._handle_start,
                "monitor.stop": self._handle_stop,
                "reconnect": self._handle_reconnect,
            },
            logs_dir=self.logs_dir,
            version=self._version,
            on_change=self.reload,
        )
        self._server = PipeServer(self._api.handle, pipe_name=self._pipe_name)
        self._server.start()

        self._build_monitor()
        self._start_monitor()
        self._logger.info(
            "服务运行时已启动（版本 %s，日志 %s，管道 %s）", self._version, self.logs_dir, self._pipe_name
        )

    def stop(self) -> None:
        state = self._stop_monitor(wait=self._stop_grace)
        if state == "stopping":
            self._logger.error("监控线程在 %.0f 秒内未退出，仍保留引用后再停止服务", self._stop_grace)
        if self._server:
            self._server.stop()
            self._server.join(timeout=3)
            self._server = None
        self.status.state = State.STOPPED.value
        self._logger.info("服务运行时已停止")

    @property
    def logs_dir(self) -> Path:
        return self._store.base_dir / "logs"

    # ---- 配置 ----
    @property
    def config(self) -> Config:
        return self._config

    def reload(self) -> None:
        """配置变更后热加载：先确认旧线程退出，再重建监控器并启动。"""
        try:
            new_config = self._store.load()
        except Exception as exc:
            self._logger.error("重载配置失败，保留旧配置: %s", exc)
            return

        if self._stop_monitor(wait=self._stop_grace) != "stopped":
            self._logger.error("旧监控线程未退出，跳过本次热加载")
            return

        self._config = new_config
        self._apply_log_level()
        self._build_monitor()
        self._start_monitor()
        self._logger.info("配置已重载")

    def _apply_log_level(self) -> None:
        import logging

        level = getattr(logging, str(self._config.logging.level).upper(), logging.INFO)
        self._logger.setLevel(level)
        for handler in self._logger.handlers:
            handler.setLevel(level)

    # ---- 组件装配 ----
    def _build_monitor(self) -> None:
        http = self._http_override or HttpClient(
            default_timeout=self._config.monitor.connect_timeout_seconds
        )
        self._http = http
        self._portal = self._portal_override or PortalClient(
            http, timeout=self._config.monitor.connect_timeout_seconds
        )
        self._recognize = self._recognize_override or SubprocessOcr(
            frozen_worker_command() if is_frozen() else None
        )
        self._check = self._check_online_override or self._check_online
        self._monitor = Monitor(
            self._config,
            self._portal,
            self._recognize,
            self._check,
            on_state=self._on_state,
            stop_event=self._stop_event,
            wake_event=self._wake_event,
        )

    def _check_online(self) -> bool:
        try:
            response = self._http.get(
                self._config.monitor.check_url,
                timeout=self._config.monitor.connect_timeout_seconds,
            )
            return response.status == 204
        except NetworkError:
            return False

    # ---- 监控线程 ----
    def _start_monitor(self) -> str:
        thread = self._monitor_thread
        if thread is not None and thread.is_alive():
            # 旧线程还没退出（例如卡在 OCR），绝不再起一个
            return "stopping"
        self._stop_event.clear()
        self._wake_event.clear()
        assert self._monitor is not None
        self._monitor_thread = threading.Thread(
            target=self._run_monitor,
            name="MonitorLoop",
            daemon=True,
        )
        self._monitor_thread.start()
        return "running"

    def _run_monitor(self) -> None:
        try:
            self._monitor.run()
        finally:
            if self._monitor_thread is threading.current_thread():
                self._monitor_thread = None

    def _stop_monitor(self, wait: float = 1.0) -> str:
        """请求停止；`wait` 秒内没退出则返回 "stopping" 并保留线程引用。"""
        thread = self._monitor_thread
        if thread is None or not thread.is_alive():
            self._monitor_thread = None
            self.status.state = State.STOPPED.value
            return "stopped"

        self._stop_event.set()
        self._wake_event.set()
        thread.join(timeout=wait)
        if thread.is_alive():
            return "stopping"  # 引用必须保留，防止重复启动
        self._monitor_thread = None
        self.status.state = State.STOPPED.value
        return "stopped"

    # ---- 动作 ----
    def _handle_start(self) -> str:
        return self._start_monitor()

    def _handle_stop(self) -> str:
        result = self._stop_monitor(wait=1.0)
        if result == "stopped":
            self._logger.info("监控已手动停止")
        else:
            self._logger.warning("监控正在停止，旧线程尚未退出")
        return result

    def _handle_reconnect(self) -> str:
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._wake_event.set()
            return "queued"
        return self._start_monitor()

    # ---- 状态同步 ----
    def _on_state(self, result: CycleResult) -> None:
        self.status.state = result.state.value
        self.status.last_check_at = _now()
        self.status.consecutive_failures = result.consecutive_failures
        self.status.last_error = "" if result.state is State.ONLINE else result.detail

        if result.state is State.ONLINE:
            delay = float(self._config.monitor.check_interval_seconds)
        elif self._monitor is not None:
            delay = self._monitor.compute_backoff()
        else:
            delay = float(self._config.monitor.check_interval_seconds)
        self.status.next_check_at = _after(delay)

        if result.state.value != self._last_state:
            self._last_state = result.state.value
            if result.detail:
                self._logger.info("状态 -> %s（%s）", result.state.value, result.detail)
            else:
                self._logger.info("状态 -> %s", result.state.value)

    # ---- 运行辅助（供控制台模式与测试）----
    def wait(self, stop_event: threading.Event, timeout: float | None = None) -> None:
        stop_event.wait(timeout)
