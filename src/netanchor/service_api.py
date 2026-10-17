"""IPC 命令分发。

写操作不再要求管理员：安全边界改由"凭据密文绑定门户地址"承担——
地址一变旧密文即不可解，服务不会把密码发往新地址（见 docs/architecture.md §20）。
调用者的提权状态仍会记入审计日志。
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .config import CARRIER_SUFFIX, Config
from .config_store import ConfigStore
from .ipc import PROTOCOL_VERSION

READ_COMMANDS = {"status", "config.get", "logs.path", "version"}
WRITE_COMMANDS = {"config.set", "creds.set", "monitor.start", "monitor.stop", "reconnect"}

audit = logging.getLogger("netanchor.audit")


class ReauthRequired(ValueError):
    """门户地址变更时必须同时重新输入密码。"""


def _mask_username(username: str) -> str:
    if not username:
        return ""
    return username[:3] + "***" if len(username) > 3 else "***"


@dataclass
class RuntimeStatus:
    state: str = "stopped"
    last_check_at: str | None = None
    next_check_at: str | None = None
    consecutive_failures: int = 0
    last_error: str = ""
    portal_in_use: str = ""
    started_at: float = field(default_factory=time.time)

    def as_payload(self) -> dict:
        payload = asdict(self)
        payload["started_at"] = round(self.started_at, 3)
        payload["uptime_seconds"] = int(time.time() - self.started_at)
        return payload


def masked_config(config: Config) -> dict:
    payload = config.to_dict()
    account = payload["account"]
    account.pop("password", None)
    if account.get("password_protected"):
        account["password_protected"] = "***"
    return payload


class ServiceApi:
    def __init__(
        self,
        store: ConfigStore,
        status: RuntimeStatus | None = None,
        actions: dict[str, Callable[[], str]] | None = None,
        logs_dir: Path | None = None,
        version: str = "0.1.0",
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._status = status or RuntimeStatus()
        self._actions = actions or {}
        self._logs_dir = logs_dir or (store.base_dir / "logs")
        self._version = version
        self._on_change = on_change

    # ---- 入口 ----
    def handle(self, request: dict, is_admin: bool) -> dict:
        request_id = str(request.get("id", "?"))
        cmd = request.get("cmd")
        data = request.get("data") or {}

        if not isinstance(cmd, str):
            return self._error(request_id, "invalid_request", "缺少 cmd")
        if cmd not in READ_COMMANDS and cmd not in WRITE_COMMANDS:
            return self._error(request_id, "unknown_command", f"未知命令: {cmd}")

        try:
            payload = self._dispatch(cmd, data)
        except ReauthRequired as exc:
            return self._error(request_id, "reauth_required", str(exc))
        except ValueError as exc:
            return self._error(request_id, "invalid_config", str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._error(request_id, "internal", f"{type(exc).__name__}: {exc}")
        if cmd in WRITE_COMMANDS:
            audit.info("cmd=%s admin=%s result=ok", cmd, is_admin)
        return {"v": PROTOCOL_VERSION, "id": request_id, "ok": True, "data": payload}

    def _dispatch(self, cmd: str, data: dict):
        if cmd == "status":
            return self._status.as_payload()
        if cmd == "version":
            return {"version": self._version}
        if cmd == "logs.path":
            return {"path": str(self._logs_dir)}
        if cmd == "config.get":
            return {"config": masked_config(self._store.load())}
        if cmd == "config.set":
            return self._set_config(data)
        if cmd == "creds.set":
            return self._set_credentials(data)

        action = self._actions.get(cmd)
        if action is None:
            raise RuntimeError(f"命令未实现: {cmd}")
        return {"result": action()}

    # ---- 写命令 ----
    def _set_config(self, data: dict) -> dict:
        incoming = data.get("config")
        if not isinstance(incoming, dict):
            raise ValueError("config 必须是对象")
        password = data.get("password") or ""
        current = self._store.load(decrypt=False)
        try:
            candidate = Config.from_dict(incoming)
        except TypeError as exc:
            raise ValueError(f"配置字段非法: {exc}") from exc

        candidate.account.password = ""
        candidate.account.password_protected = current.account.password_protected
        candidate.account.password_context = current.account.password_context
        candidate.require_valid()

        portal_changed = candidate.portal_fingerprint != current.portal_fingerprint
        if portal_changed and not password:
            raise ReauthRequired("门户地址已变更，需要同时重新输入密码")
        if password:
            candidate.account.password = password

        self._store.save(candidate)
        self._audit("config.set", portal_changed=portal_changed, password_set=bool(password))
        self._notify_change()
        return {"saved": True, "portal_changed": portal_changed, "password_rebound": bool(password)}

    def _set_credentials(self, data: dict) -> dict:
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        carrier = str(data.get("carrier", "校园其他"))
        if not username:
            raise ValueError("username 不能为空")
        if not password:
            raise ValueError("password 不能为空")
        if carrier not in CARRIER_SUFFIX:
            raise ValueError(f"未知运营商: {carrier}")
        self._store.set_credentials(username, password, carrier)
        self._audit("creds.set", username=_mask_username(username), carrier=carrier)
        self._notify_change()
        return {"saved": True}

    @staticmethod
    def _audit(cmd: str, **details) -> None:
        rendered = " ".join(f"{key}={value}" for key, value in details.items())
        audit.info("cmd=%s %s", cmd, rendered)

    def _notify_change(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # 热加载失败不应让写操作报错
                pass

    @staticmethod
    def _error(request_id: str, code: str, message: str) -> dict:
        return {
            "v": PROTOCOL_VERSION,
            "id": request_id,
            "ok": False,
            "error": {"code": code, "message": message},
        }
