"""跨模块共享的常量（避免 service_main / cli / ipc 互相 import 成环）。"""

SERVICE_NAME = "NetAnchorCampus"
SERVICE_DISPLAY_NAME = "NetAnchor-Campus 校园网自动重连"
SERVICE_DESCRIPTION = "检测断网并通过 Dr.COM 门户自动重连；配置与凭据由服务统一管理。"

PIPE_NAME = r"\\.\pipe\NetAnchor.Campus.v1"
