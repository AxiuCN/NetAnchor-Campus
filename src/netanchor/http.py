"""HTTP 访问层：显式直连（不读系统代理）、JSONP 解析、统一错误分类。

校园网请求必须绕开系统代理：真机常开代理，而门户与取码接口在局域网内。
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .errors import NetworkError

USER_AGENT = "NetAnchor-Campus/0.1"


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class HttpClient:
    """一个带 Cookie 的直连客户端。

    `ProxyHandler({})` 是刻意的：urllib 默认会通过 `getproxies()` 读取系统/环境代理，
    在校园网场景下会把校内请求劫持到代理上。
    """

    def __init__(self, default_timeout: float = 5.0, user_agent: str = USER_AGENT) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )
        self._opener.addheaders = [("User-Agent", user_agent)]
        self._timeout = default_timeout

    def get(self, url: str, timeout: float | None = None) -> Response:
        effective = timeout if timeout is not None else self._timeout
        try:
            with self._opener.open(url, timeout=effective) as resp:
                return Response(
                    status=resp.status,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    body=resp.read(),
                )
        except urllib.error.HTTPError as exc:
            # 4xx/5xx 不是网络问题，交给协议层判断
            return Response(
                status=exc.code,
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
                body=exc.read() if hasattr(exc, "read") else b"",
            )
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NetworkError(f"请求失败: {reason}") from exc


def jsonp_callback(now_ms: int | None = None) -> str:
    """门户要求 JSONP 回调名，形如 dr<毫秒时间戳>。"""
    return f"dr{int(time.time() * 1000) if now_ms is None else now_ms}"


_JSONP_BODY = re.compile(r"\{.*\}", re.DOTALL)


def parse_jsonp(text: str) -> dict | None:
    """解析 JSONP / 纯 JSON；无法解析时返回 None（由调用方判为协议错误）。"""
    if not text or not text.strip():
        return None
    stripped = text.strip()
    candidates = [stripped]
    match = _JSONP_BODY.search(stripped)
    if match:
        candidates.append(match.group())
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None
