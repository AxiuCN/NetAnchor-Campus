"""配置存储：`%ProgramData%\\NetAnchor-Campus\\` + 受限 ACL + Machine DPAPI。

文件权限只给 SYSTEM 与 Administrators；普通用户既读不到配置，也改不了凭据，
所有读写都经由服务的 IPC 完成。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

import win32con
import win32security

from .config import Config
from .secrets import SecretError, protect, unprotect

logger = logging.getLogger(__name__)

SERVICE_DIR_NAME = "NetAnchor-Campus"
CONFIG_FILE_NAME = "config.json"
BACKUP_SUFFIX = ".bak"

_ACCESS_FULL = win32con.GENERIC_ALL


def _sid(well_known: int):
    return win32security.CreateWellKnownSid(well_known, None)


def default_base_dir() -> Path:
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / SERVICE_DIR_NAME


def _apply_restricted_dacl(path: Path, is_dir: bool) -> None:
    """替换为只含 SYSTEM + Administrators 的 DACL（不让继承把权限带回来）。"""
    system_sid = _sid(win32security.WinLocalSystemSid)
    admins_sid = _sid(win32security.WinBuiltinAdministratorsSid)

    dacl = win32security.ACL()
    flags = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE if is_dir else 0
    for sid in (system_sid, admins_sid):
        dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION, flags, _ACCESS_FULL, sid)

    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )


def describe_dacl(path: Path) -> list[tuple[str, int]]:
    """返回 [(SID 字符串, 访问掩码)]，用于测试与诊断。"""
    descriptor = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None:
        return []
    entries = []
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        entries.append((win32security.ConvertSidToStringSid(ace[2]), ace[1]))
    return entries


class ConfigStore:
    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else default_base_dir()
        self.path = self.base_dir / CONFIG_FILE_NAME
        self.backup_path = self.base_dir / (CONFIG_FILE_NAME + BACKUP_SUFFIX)

    # ---- 目录与权限 ----
    def ensure_dir(self) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        _apply_restricted_dacl(self.base_dir, is_dir=True)
        return self.base_dir

    # ---- 读写 ----
    def load(self, decrypt: bool = True) -> Config:
        if not self.path.exists():
            return Config()
        with open(self.path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        config = Config.from_dict(data if isinstance(data, dict) else {})
        if not (decrypt and config.account.password_protected):
            return config

        if config.account.password_context != config.portal_fingerprint:
            # 门户地址变了：旧密文在密码学上已不可用，等用户重新输入密码
            logger.warning("门户地址与凭据密文绑定的地址不一致，需要重新输入密码")
            return config
        try:
            config.account.password = unprotect(
                config.account.password_protected,
                context=config.account.password_context,
            )
        except SecretError as exc:
            raise SecretError(f"配置中的密码无法解密: {exc}") from exc
        return config

    def save(self, config: Config) -> Path:
        if config.account.password:
            context = config.portal_fingerprint
            config.account.password_protected = protect(config.account.password, context=context)
            config.account.password_context = context
        config.require_valid()
        return self._write_payload(config.to_dict())

    def init_template(self) -> Path:
        """安装时写入占位配置（不校验，等用户填真实地址与账号）。"""
        if self.path.exists():
            return self.path
        return self._write_payload(Config().to_dict())

    def _write_payload(self, data: dict) -> Path:
        self.ensure_dir()
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        fd, temp_name = tempfile.mkstemp(dir=self.base_dir, prefix=".config-", suffix=".tmp")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _apply_restricted_dacl(temp_path, is_dir=False)
            if self.path.exists():
                os.replace(self.path, self.backup_path)
            os.replace(temp_path, self.path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        _apply_restricted_dacl(self.path, is_dir=False)
        return self.path

    # ---- 凭据 ----
    def set_credentials(self, username: str, password: str, carrier: str) -> Config:
        config = self.load(decrypt=False)
        config.account.username = username
        config.account.carrier = carrier
        config.account.password = password
        config.account.password_protected = ""
        self.save(config)  # save 会按当前门户指纹重新加密
        return config
