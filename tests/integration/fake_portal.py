"""L2 桩门户：两个本地 HTTP 服务分别扮演登录端口与取码端口。"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GIF_BYTES = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"


class FakePortal:
    def __init__(
        self,
        accepted_text: str = "1234",
        login_result: str = "1",
        login_message: str = "ok",
        getcode_status: int = 200,
        login_raw: str | None = None,
        check_delay: float = 0.0,
    ) -> None:
        self.accepted_text = accepted_text
        self.login_result = login_result
        self.login_message = login_message
        self.getcode_status = getcode_status
        self.login_raw = login_raw
        self.check_delay = check_delay
        self.check_calls = 0

        self._login = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._login.role = "login"
        self._login.portal = self
        self._eportal = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._eportal.role = "eportal"
        self._eportal.portal = self
        self._threads = [
            threading.Thread(target=self._login.serve_forever, daemon=True),
            threading.Thread(target=self._eportal.serve_forever, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    @property
    def login_port(self) -> int:
        return self._login.server_address[1]

    @property
    def eportal_port(self) -> int:
        return self._eportal.server_address[1]

    def stop(self) -> None:
        for server in (self._login, self._eportal):
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _send(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        role = self.server.role
        portal: FakePortal = self.server.portal

        if role == "login":
            if parsed.path == "/":
                self._send(
                    200,
                    b"<html><title>logout</title></html>",
                    {"Server": "DrcomServer1.0", "Content-Type": "text/html; charset=gbk"},
                )
                return
            if parsed.path == "/drcom/login":
                if portal.login_raw is not None:
                    self._send(200, portal.login_raw.encode(), {"Server": "DrcomServer1.0"})
                    return
                payload = {"result": portal.login_result}
                if portal.login_result == "1":
                    payload["msga"] = portal.login_message
                else:
                    payload["msga"] = portal.login_message
                body = f"dr123({json.dumps(payload)})".encode()
                self._send(200, body, {"Server": "DrcomServer1.0", "Content-Type": "text/html"})
                return
            self._send(404, b"not found")
            return

        # eportal 角色
        if parsed.path == "/eportal/":
            action = (query.get("a") or [""])[0]
            if action == "getCode":
                if portal.getcode_status != 200:
                    self._send(portal.getcode_status, b"error")
                    return
                self._send(
                    200,
                    GIF_BYTES,
                    {"Server": "OMPXY/1.4.7", "Content-Type": "image/gif",
                     "Set-Cookie": "PHPSESSID=test; path=/"},
                )
                return
            if action == "check_captcha":
                portal.check_calls += 1
                if portal.check_delay:
                    time.sleep(portal.check_delay)
                text = (query.get("captcha") or [""])[0]
                result = "1" if text == portal.accepted_text else "0"
                body = f"dr123({json.dumps({'result': result})})".encode()
                self._send(200, body, {"Content-Type": "text/html"})
                return
        self._send(404, b"not found")
