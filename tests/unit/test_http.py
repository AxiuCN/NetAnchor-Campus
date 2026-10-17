import pytest

from netanchor.http import jsonp_callback, parse_jsonp


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"result":"1"}', {"result": "1"}),
        ('dr1234567890({"result":"0"})', {"result": "0"}),
        ('dr123({"result":"1","msga":"ok"})', {"result": "1", "msga": "ok"}),
        ('  \n dr99({"result":"1"}) \n ', {"result": "1"}),
    ],
)
def test_parse_jsonp_valid(text, expected):
    assert parse_jsonp(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "null", "[1,2]", "dr1()", "dr1(({bad}))", "not json"])
def test_parse_jsonp_invalid(text):
    assert parse_jsonp(text) is None


def test_jsonp_callback_format():
    name = jsonp_callback(1700000000123)
    assert name == "dr1700000000123"
    assert jsonp_callback().startswith("dr")
    assert jsonp_callback()[2:].isdigit()
