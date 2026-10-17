"""内存测量工具：对指定进程定时采样 WorkingSet / Private / Peak，输出 CSV 与汇总。

用法：
    python tools/measure_memory.py --pid 1234 --seconds 60 --interval 1 --out .dsh/mem-S1.csv

只依赖标准库（ctypes 调 psapi），可在任意 Python 3.8+ 下运行。
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time

import ctypes
from ctypes import wintypes

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

psapi = ctypes.WinDLL("psapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


psapi.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
    wintypes.DWORD,
]
psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

MB = 1024 * 1024


def sample(pid: int) -> dict | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return {
            "working_set_mb": counters.WorkingSetSize / MB,
            "private_mb": counters.PrivateUsage / MB,
            "peak_working_set_mb": counters.PeakWorkingSetSize / MB,
        }
    finally:
        kernel32.CloseHandle(handle)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--settle", type=int, default=20, help="前 N 秒不计入均值")
    args = ap.parse_args()

    rows = []
    start = time.time()
    while time.time() - start < args.seconds:
        m = sample(args.pid)
        if m is None:
            print(f"进程 {args.pid} 不可访问（已退出或无权限）", file=sys.stderr)
            return 2
        rows.append({"t": round(time.time() - start, 1), **m})
        time.sleep(args.interval)

    stable = [r for r in rows if r["t"] >= args.settle] or rows
    ws = [r["working_set_mb"] for r in stable]
    pv = [r["private_mb"] for r in stable]
    peak = max(r["peak_working_set_mb"] for r in rows)

    print(f"pid={args.pid} 采样={len(rows)} 稳定段={len(stable)}")
    print(f"WorkingSet  均值 {statistics.mean(ws):.1f} MB  峰值 {max(ws):.1f} MB")
    print(f"Private     均值 {statistics.mean(pv):.1f} MB  峰值 {max(pv):.1f} MB")
    print(f"PeakWorkingSet(进程历史) {peak:.1f} MB")

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"明细: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
