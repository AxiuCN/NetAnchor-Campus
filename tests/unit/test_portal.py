"""门户地址规范化与"候选全失败"的诊断信息。"""
from netanchor.config import PortalCandidate
from netanchor.errors import NetworkError
from netanchor.http import Response
from netanchor.portal import PortalClient, _base, _eportal, normalize_host


class StubHttp:
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.urls: list[str] = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.result


def _ok_response() -> Response:
    return Response(status=200, headers={"server": "DrcomServer1.0"}, body=b"<html/>")


def test_normalize_host_strips_scheme_and_trailing_slash():
    # 真机配置里出现过这种值，直接拼协议头会变成 http://http://...
    assert normalize_host("http://192.168.115.8/") == "192.168.115.8"
    assert normalize_host("  https://portal.local/path  ") == "portal.local"
    assert normalize_host("10.0.0.1") == "10.0.0.1"
    assert normalize_host("") == ""


def test_base_builds_valid_url_from_full_url_host():
    candidate = PortalCandidate(
        name="主用", host="http://192.168.115.8/", login_port=80, eportal_port=801
    )
    assert _base(candidate) == "http://192.168.115.8"
    assert _eportal(candidate) == "http://192.168.115.8:801"


def test_base_keeps_custom_login_port():
    candidate = PortalCandidate(name="主用", host="10.1.1.1", login_port=8080, eportal_port=801)
    assert _base(candidate) == "http://10.1.1.1:8080"


def test_pick_candidate_records_reason_when_all_fail():
    http = StubHttp(error=NetworkError("请求失败: [Errno 11001] getaddrinfo failed"))
    client = PortalClient(http)
    candidate = PortalCandidate(
        name="主用", host="http://192.168.115.8/", login_port=80, eportal_port=801
    )

    assert client.pick_candidate([candidate]) is None
    assert "192.168.115.8" in client.last_probe_error
    assert "NetworkError" in client.last_probe_error


def test_pick_candidate_returns_candidate_and_clears_reason():
    http = StubHttp(result=_ok_response())
    client = PortalClient(http)
    candidate = PortalCandidate(name="主用", host="10.1.1.1", login_port=80, eportal_port=801)

    assert client.pick_candidate([candidate]) is candidate
    assert client.last_probe_error == ""
    assert http.urls == ["http://10.1.1.1/"]
