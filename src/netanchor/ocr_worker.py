"""OCR 工作进程：按需由服务拉起，读一张图、返回一次识别结果后退出。

协议（stdin/stdout 二进制）：
  请求：4 字节大端长度 + 图片字节
  响应：4 字节大端长度 + UTF-8 JSON {"text": str, "error": str|null}
"""
from __future__ import annotations

import json
import struct
import sys

from .ocr import OcrEngine


def _read_exact(stream, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def main() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    engine: OcrEngine | None = None

    while True:
        header = _read_exact(stdin, 4)
        if len(header) < 4:
            return 0
        (size,) = struct.unpack(">I", header)
        image = _read_exact(stdin, size)
        if len(image) < size:
            return 1

        try:
            if engine is None:
                engine = OcrEngine()
            response = {"text": engine.recognize(image), "error": None}
        except Exception as exc:  # 工作进程不崩溃，把错误交回服务
            response = {"text": "", "error": f"{type(exc).__name__}: {exc}"}

        body = json.dumps(response, ensure_ascii=False).encode("utf-8")
        stdout.write(struct.pack(">I", len(body)) + body)
        stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
