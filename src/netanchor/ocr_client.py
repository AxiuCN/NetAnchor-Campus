"""OCR 客户端：按需拉起工作进程，单次识别一次，进程退出后不驻留内存。"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

from .errors import ProtocolError

DEFAULT_TIMEOUT = 30.0


def default_worker_command() -> list[str]:
    """开发态：用当前解释器跑模块；打包后由调用方传入 worker exe 路径。"""
    return [sys.executable, "-m", "netanchor.ocr_worker"]


class SubprocessOcr:
    def __init__(self, command: list[str] | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._command = command or default_worker_command()
        self._timeout = timeout

    @property
    def command(self) -> list[str]:
        return list(self._command)

    def recognize(self, image: bytes) -> str:
        payload = struct.pack(">I", len(image)) + image
        try:
            completed = subprocess.run(
                self._command,
                input=payload,
                capture_output=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProtocolError("OCR 工作进程超时") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", "replace").strip()
            raise ProtocolError(f"OCR 工作进程退出码 {completed.returncode}: {stderr[:200]}")

        out = completed.stdout
        if len(out) < 4:
            raise ProtocolError("OCR 工作进程无响应")
        (size,) = struct.unpack(">I", out[:4])
        body = out[4:4 + size]
        if len(body) < size:
            raise ProtocolError("OCR 工作进程响应被截断")
        try:
            payload_obj = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("OCR 工作进程响应不是合法 JSON") from exc
        if payload_obj.get("error"):
            raise ProtocolError(f"OCR 失败: {payload_obj['error']}")
        return str(payload_obj.get("text", ""))

    def __call__(self, image: bytes) -> str:
        return self.recognize(image)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def frozen_worker_command(exe: Path | None = None) -> list[str]:
    """打包后：与服务同目录的 NetAnchor.Ocr.exe。"""
    target = exe or (Path(sys.executable).parent / "NetAnchor.Ocr.exe")
    return [str(target)]
