import json
import struct

import pytest

from netanchor.ipc import MAX_FRAME, IpcError, decode_frame, encode_frame


def test_frame_roundtrip():
    payload = {"v": 1, "id": "x", "cmd": "status", "data": {"中文": "值"}}
    assert decode_frame(encode_frame(payload)) == payload


def test_frame_rejects_short_header():
    with pytest.raises(IpcError):
        decode_frame(b"\x01\x02")


def test_frame_rejects_truncated_body():
    body = json.dumps({"a": 1}).encode()
    frame = struct.pack("<I", len(body) + 10) + body
    with pytest.raises(IpcError):
        decode_frame(frame)


def test_frame_rejects_oversize_declaration():
    frame = struct.pack("<I", MAX_FRAME + 1) + b"{}"
    with pytest.raises(IpcError):
        decode_frame(frame)


def test_frame_rejects_non_json():
    body = b"not json"
    with pytest.raises(IpcError):
        decode_frame(struct.pack("<I", len(body)) + body)


def test_frame_rejects_non_object():
    body = b"[1,2]"
    with pytest.raises(IpcError):
        decode_frame(struct.pack("<I", len(body)) + body)


def test_encode_rejects_oversize_payload():
    with pytest.raises(IpcError):
        encode_frame({"blob": "x" * (MAX_FRAME + 10)})
