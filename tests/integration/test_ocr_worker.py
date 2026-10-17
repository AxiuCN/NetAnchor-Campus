import os
import sys
from pathlib import Path

import pytest

from netanchor.errors import ProtocolError
from netanchor.ocr_client import SubprocessOcr, default_worker_command

HERE = Path(__file__).parent


def test_subprocess_ocr_with_fake_worker():
    client = SubprocessOcr([sys.executable, str(HERE / "fake_worker.py")], timeout=20)
    assert client.recognize(b"not-a-real-image") == "4321"


def test_subprocess_ocr_reports_crash():
    client = SubprocessOcr([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=20)
    with pytest.raises(ProtocolError):
        client.recognize(b"x")


def test_subprocess_ocr_reports_worker_error():
    code = (
        "import json,struct,sys;"
        "h=sys.stdin.buffer.read(4);(n,)=struct.unpack('>I',h);sys.stdin.buffer.read(n);"
        "b=json.dumps({'text':'','error':'boom'}).encode();"
        "sys.stdout.buffer.write(struct.pack('>I',len(b))+b);sys.stdout.buffer.flush()"
    )
    client = SubprocessOcr([sys.executable, "-c", code], timeout=20)
    with pytest.raises(ProtocolError):
        client.recognize(b"x")


@pytest.mark.skipif(
    os.environ.get("NA_TEST_REAL_OCR") != "1",
    reason="需要 ddddocr 与真实验证码样本，设置 NA_TEST_REAL_OCR=1 才跑",
)
def test_real_worker_on_sample():
    sample = Path(r"D:\NetAnchor-Campus\.dsh\probe\captcha\sample1.gif")
    if not sample.exists():
        pytest.skip("样本不存在")
    client = SubprocessOcr(default_worker_command(), timeout=60)
    text = client.recognize(sample.read_bytes())
    assert text.isdigit()
