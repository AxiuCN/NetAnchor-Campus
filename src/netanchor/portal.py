"""Dr.COM 门户协议：候选探测、取码、验证码校验、登录。

协议事实（来自旧实现与本地实测）：
- 在线判定与门户探测走 `http://<host>:<login_port>/`，响应头 Server 含 DrcomServer1.0
- 取码走 `http://<host>:<eportal_port>/eportal/?c=main&a=getCode`
- 校验码走同一端口 `c=Portal&a=check_captcha`，参数名是 `captcha`，`result == "1"` 为通过
- 校验是消耗性的：同一张码只能判一次，失败必须重新取码
- 登录走 `http://<host>:<login_port>/drcom/login`，JSONP 返回 `result == "1"` 为成功
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import dataclass

from .config import PortalCandidate
from .errors import ProtocolError
from .http import HttpClient, jsonp_callback, parse_jsonp

logger = logging.getLogger(__name__)

LOGIN_EXTRA_PARAMS = {
    "0MKKey": "123456",
    "R1": "0",
    "R3": "0",
    "R6": "0",
    "para": "00",
    "v6ip": "",
}


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    code: str
    message: str


def normalize_host(host: str) -> str:
    """把用户可能填成完整 URL 的 host 收敛成主机名。

    配置里出现过 `http://192.168.115.8/`：再拼一次协议头就变成
    `http://http://192.168.115.8/`，探测恒定失败，而日志里只剩"候选均不可达"，
    看起来像断网。这里只规整**请求侧**，不改存储值——存储值参与凭据指纹，
    改了会让已保存的密码失效。
    """
    value = (host or "").strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    return value.strip()


def _base(candidate: PortalCandidate) -> str:
    host = normalize_host(candidate.host)
    if candidate.login_port == 80:
        return f"http://{host}"
    return f"http://{host}:{candidate.login_port}"


def _eportal(candidate: PortalCandidate) -> str:
    return f"http://{normalize_host(candidate.host)}:{candidate.eportal_port}"


class PortalClient:
    def __init__(self, http: HttpClient, timeout: float = 5.0) -> None:
        self._http = http
        self._timeout = timeout
        # 最近一轮"所有候选都失败"的具体原因，供上层写进状态与日志
        self.last_probe_error = ""
        self._logged_probe_error = ""

    def probe(self, candidate: PortalCandidate, timeout: float | None = None) -> bool:
        """探测候选门户是否可用：200 且 Server 头含 DrcomServer1.0。"""
        response = self._http.get(_base(candidate) + "/", timeout=timeout or self._timeout)
        server = response.headers.get("server", "").lower()
        ok = response.status == 200 and "drcomserver" in server
        logger.debug("门户探测 %s -> status=%s server=%s", candidate.host, response.status, server)
        return ok

    def pick_candidate(
        self, candidates: list[PortalCandidate], timeout: float | None = None
    ) -> PortalCandidate | None:
        failures: list[str] = []
        for candidate in candidates:
            try:
                if self.probe(candidate, timeout=timeout):
                    self.last_probe_error = ""
                    self._logged_probe_error = ""
                    return candidate
            except ProtocolError as exc:
                failures.append(f"{normalize_host(candidate.host)}: {exc}")
            except Exception as exc:  # 网络错误逐个跳过，交给上层判断
                failures.append(
                    f"{normalize_host(candidate.host)}: {type(exc).__name__}: {exc}"
                )
        self.last_probe_error = "；".join(failures)
        # 只在原因变化时记一条：离线时每轮都刷同样的失败没有意义
        if self.last_probe_error != self._logged_probe_error:
            self._logged_probe_error = self.last_probe_error
            logger.warning("门户候选均探测失败：%s", self.last_probe_error)
        return None

    def fetch_captcha(self, candidate: PortalCandidate) -> bytes:
        url = f"{_eportal(candidate)}/eportal/?c=main&a=getCode&v={int(time.time() * 1000)}"
        response = self._http.get(url, timeout=self._timeout)
        if response.status != 200:
            raise ProtocolError(f"取码返回 {response.status}")
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            raise ProtocolError(f"取码返回非图片: {content_type}")
        if not response.body:
            raise ProtocolError("取码返回空内容")
        return response.body

    def check_captcha(self, candidate: PortalCandidate, text: str) -> bool:
        query = urllib.parse.urlencode(
            {
                "c": "Portal",
                "a": "check_captcha",
                "callback": jsonp_callback(),
                "captcha": text,
                "_": int(time.time() * 1000),
            }
        )
        response = self._http.get(f"{_eportal(candidate)}/eportal/?{query}", timeout=self._timeout)
        if response.status != 200:
            raise ProtocolError(f"校验码返回 {response.status}")
        payload = parse_jsonp(response.text)
        if payload is None:
            raise ProtocolError("校验码响应无法解析")
        return str(payload.get("result")) == "1"

    def login(self, candidate: PortalCandidate, username: str, password: str) -> LoginResult:
        params = {
            "callback": jsonp_callback(),
            "DDDDD": username,
            "upass": password,
            "_": int(time.time() * 1000),
        }
        params.update(LOGIN_EXTRA_PARAMS)
        url = f"{_base(candidate)}/drcom/login?{urllib.parse.urlencode(params)}"
        # 注意：url 含密码，任何日志都不得输出 url
        response = self._http.get(url, timeout=self._timeout)
        if response.status != 200:
            return LoginResult(False, str(response.status), "登录返回非 200")
        payload = parse_jsonp(response.text)
        if payload is None:
            raise ProtocolError("登录响应无法解析")
        code = str(payload.get("result"))
        message = str(payload.get("msga") or payload.get("msg") or "")
        ok = code == "1"
        logger.info("登录结果 result=%s msg=%s", code, message)
        return LoginResult(ok, code, message)
