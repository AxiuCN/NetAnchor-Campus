"""配置模型：加载、校验、原子保存。

P2 只做文件读写；凭据加密（Machine DPAPI）与 ProgramData ACL 在 P3 接入。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_VERSION = 1

# 门户页面（a41.js）给出的运营商后缀
CARRIER_SUFFIX = {
    "校园用户": "@xyw",
    "校园电信": "@dx",
    "校园联通": "@lt",
    "校园其他": "",
}

PORTAL_PLACEHOLDER = "<portal-host>"


def default_config_path() -> Path:
    base = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    return Path(base) / "NetAnchor-Campus" / "config.json"


@dataclass
class PortalCandidate:
    name: str = "主用"
    host: str = PORTAL_PLACEHOLDER
    login_port: int = 80
    eportal_port: int = 801


@dataclass
class PortalSettings:
    candidates: list[PortalCandidate] = field(default_factory=lambda: [PortalCandidate()])
    probe_timeout_seconds: float = 3.0


def portal_fingerprint(portal: PortalSettings) -> str:
    """门户地址指纹：凭据密文的加密上下文，地址一变旧密文即失效。"""
    parts = sorted(
        f"{item.host.strip().lower()}|{item.login_port}|{item.eportal_port}"
        for item in portal.candidates
    )
    return ";".join(parts)


@dataclass
class AccountSettings:
    username: str = ""
    carrier: str = "校园其他"
    # 运行期明文，不落盘
    password: str = ""
    # 落盘的加密值；加密上下文绑定门户地址（见 portal_fingerprint）
    password_protected: str = ""
    # 生成密文时使用的门户指纹；与本机当前地址不一致时视为不可用
    password_context: str = ""


@dataclass
class MonitorSettings:
    check_url: str = "http://connect.rom.miui.com/generate_204"
    check_interval_seconds: int = 30
    connect_timeout_seconds: float = 5.0
    max_captcha_attempts: int = 3
    backoff_seconds: list[int] = field(default_factory=lambda: [5, 15, 30, 60, 120, 300])


@dataclass
class OcrSettings:
    mode: str = "subprocess"  # subprocess（默认，见 docs/模块-服务.md）| inprocess
    idle_exit_seconds: int = 60


@dataclass
class LoggingSettings:
    level: str = "INFO"
    max_files: int = 7


@dataclass
class Config:
    config_version: int = CONFIG_VERSION
    portal: PortalSettings = field(default_factory=PortalSettings)
    account: AccountSettings = field(default_factory=AccountSettings)
    monitor: MonitorSettings = field(default_factory=MonitorSettings)
    ocr: OcrSettings = field(default_factory=OcrSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)

    # ---- 派生 ----
    @property
    def carrier_suffix(self) -> str:
        return CARRIER_SUFFIX.get(self.account.carrier, "")

    @property
    def effective_username(self) -> str:
        return f"{self.account.username}{self.carrier_suffix}"

    @property
    def portal_fingerprint(self) -> str:
        return portal_fingerprint(self.portal)

    # ---- 校验 ----
    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.portal.candidates:
            errors.append("portal.candidates 不能为空")
        for item in self.portal.candidates:
            if not item.host or item.host == PORTAL_PLACEHOLDER:
                errors.append(f"portal 候选 {item.name!r} 未填写 host")
            for label, port in (("login_port", item.login_port), ("eportal_port", item.eportal_port)):
                if not (0 < port < 65536):
                    errors.append(f"{item.name!r}.{label} 端口非法: {port}")
        if self.monitor.check_interval_seconds <= 0:
            errors.append("monitor.check_interval_seconds 必须为正数")
        if self.monitor.connect_timeout_seconds <= 0:
            errors.append("monitor.connect_timeout_seconds 必须为正数")
        if self.monitor.max_captcha_attempts <= 0:
            errors.append("monitor.max_captcha_attempts 必须为正数")
        if not self.monitor.backoff_seconds:
            errors.append("monitor.backoff_seconds 不能为空")
        elif any(sec <= 0 for sec in self.monitor.backoff_seconds):
            errors.append("monitor.backoff_seconds 必须全为正数")
        if self.account.carrier not in CARRIER_SUFFIX:
            errors.append(f"未知运营商: {self.account.carrier}")
        return errors

    def require_valid(self) -> None:
        """校验失败时抛 ValueError，供保存路径统一处理。"""
        errors = self.validate()
        if errors:
            raise ValueError("；".join(errors))

    def credentials_ready(self) -> bool:
        if not self.account.username:
            return False
        if self.account.password:
            return True
        # 密文只有在其绑定的门户地址与当前配置一致时才算可用
        return bool(self.account.password_protected) and (
            self.account.password_context == self.portal_fingerprint
        )

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        data = asdict(self)
        data["account"].pop("password", None)  # 明文永不落盘
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        portal_raw = data.get("portal") or {}
        candidates = [PortalCandidate(**item) for item in portal_raw.get("candidates") or []]
        portal = PortalSettings(
            candidates=candidates or [PortalCandidate()],
            probe_timeout_seconds=float(portal_raw.get("probe_timeout_seconds", 3.0)),
        )
        account = AccountSettings(**(data.get("account") or {}))
        monitor = MonitorSettings(**(data.get("monitor") or {}))
        ocr = OcrSettings(**(data.get("ocr") or {}))
        logging_settings = LoggingSettings(**(data.get("logging") or {}))
        return cls(
            config_version=int(data.get("config_version", CONFIG_VERSION)),
            portal=portal,
            account=account,
            monitor=monitor,
            ocr=ocr,
            logging=logging_settings,
        )

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        target = Path(path) if path else default_config_path()
        if not target.exists():
            return cls()
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("配置文件根节点必须是对象")
        return cls.from_dict(data)

    def save(self, path: Path | str | None = None) -> Path:
        target = Path(path) if path else default_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return target
