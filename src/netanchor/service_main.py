"""服务入口：SCM 宿主 + 开发态控制台模式。

- 由 SCM 启动（`NetAnchor.Service.exe` 无参数、frozen）
- 安装/卸载：`NetAnchor.Service.exe install` / `remove`（必须用打包后的 exe）
- 开发调试：`python -m netanchor.service_main --console`
"""
from __future__ import annotations

import sys
import threading

import servicemanager
import win32event
import win32service
import win32serviceutil

from .constants import SERVICE_DESCRIPTION, SERVICE_DISPLAY_NAME, SERVICE_NAME
from .runtime import ServiceRuntime


class NetAnchorService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY_NAME
    _svc_description_ = SERVICE_DESCRIPTION

    def __init__(self, args):
        super().__init__(args)
        self._stop = win32event.CreateEvent(None, 0, 0, None)
        self._runtime: ServiceRuntime | None = None

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self._runtime is not None:
            try:
                self._runtime.stop()
            except Exception:  # 停止阶段不再抛异常
                pass
        win32event.SetEvent(self._stop)

    def SvcShutdown(self) -> None:
        self.SvcStop()

    def SvcDoRun(self) -> None:
        servicemanager.LogInfoMsg(f"{SERVICE_NAME} 正在启动")
        self._runtime = ServiceRuntime()
        try:
            self._runtime.start()
        except Exception as exc:  # 启动失败必须可见
            servicemanager.LogErrorMsg(f"{SERVICE_NAME} 启动失败: {exc}")
            raise
        win32event.WaitForSingleObject(self._stop, win32event.INFINITE)


def run_console() -> int:
    runtime = ServiceRuntime(console=True)
    runtime.start()
    stop = threading.Event()
    try:
        while not stop.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    if "--console" in args:
        return run_console()
    if getattr(sys, "frozen", False) and len(args) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(NetAnchorService)
        servicemanager.StartServiceCtrlDispatcher()
        return 0
    win32serviceutil.HandleCommandLine(NetAnchorService)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
