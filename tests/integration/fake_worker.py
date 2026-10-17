"""测试用假 OCR 工作进程：忽略图片内容，返回固定文本。"""
import json
import struct
import sys

header = sys.stdin.buffer.read(4)
(size,) = struct.unpack(">I", header)
sys.stdin.buffer.read(size)
body = json.dumps({"text": "4321", "error": None}).encode("utf-8")
sys.stdout.buffer.write(struct.pack(">I", len(body)) + body)
sys.stdout.buffer.flush()
