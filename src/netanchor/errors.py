"""异常类型：网络条件与协议错误分开，便于日志定位与重试策略区分。"""


class NetAnchorError(Exception):
    """本项目的错误基类。"""


class NetworkError(NetAnchorError):
    """网络层失败：超时、连接被拒、DNS 失败。属于环境条件，不是程序缺陷。"""


class ProtocolError(NetAnchorError):
    """协议层失败：状态码异常、JSONP 无法解析、字段缺失。"""
