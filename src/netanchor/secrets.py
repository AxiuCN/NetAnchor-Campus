"""凭据加密：Windows Machine DPAPI（不绑定当前用户）。

机器作用域意味着同机任何账户都能解开（只要拿得到密文），
所以密文的保护靠配置文件 ACL：仅 SYSTEM 与 Administrators 可读。
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes

CRYPTPROTECT_UI_FORBIDDEN = 0x01
CRYPTPROTECT_LOCAL_MACHINE = 0x04

crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptProtectData.restype = wintypes.BOOL
crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptUnprotectData.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [ctypes.c_void_p]


class SecretError(Exception):
    """加密/解密失败。"""


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _take(blob: DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))


def entropy_for(context: str = "") -> bytes:
    """加密上下文：把密文绑定到门户地址，地址一变旧密文即不可解。"""
    return f"netanchor-campus|{context}".encode("utf-8")


def protect(text: str, machine_scope: bool = True, context: str = "") -> str:
    if not isinstance(text, str):
        raise TypeError("protect 只接受 str")
    if not text:
        return ""
    source, _keep = _blob(text.encode("utf-8"))
    entropy = _blob(entropy_for(context))[0]
    result = DATA_BLOB()
    flags = CRYPTPROTECT_UI_FORBIDDEN | (CRYPTPROTECT_LOCAL_MACHINE if machine_scope else 0)
    if not crypt32.CryptProtectData(
        ctypes.byref(source), None, ctypes.byref(entropy), None, None, flags, ctypes.byref(result)
    ):
        raise SecretError(f"CryptProtectData 失败: {ctypes.get_last_error()}")
    return base64.b64encode(_take(result)).decode("ascii")


def unprotect(token: str, machine_scope: bool = True, context: str = "") -> str:
    if not token:
        return ""
    try:
        raw = base64.b64decode(token, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SecretError("密文不是合法 base64") from exc
    source, _keep = _blob(raw)
    entropy = _blob(entropy_for(context))[0]
    result = DATA_BLOB()
    flags = CRYPTPROTECT_UI_FORBIDDEN | (CRYPTPROTECT_LOCAL_MACHINE if machine_scope else 0)
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, ctypes.byref(entropy), None, None, flags, ctypes.byref(result)
    ):
        raise SecretError(f"CryptUnprotectData 失败: {ctypes.get_last_error()}")
    return _take(result).decode("utf-8", "replace")
