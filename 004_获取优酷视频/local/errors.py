"""输出固定业务消息，屏蔽网络及原生异常可能携带的凭据。"""

import re


class ProtocolError(ValueError):
    """由适配层生成的固定错误码及脱敏诊断；禁止包含原始响应、签名 URL 或授权材料。"""


def safe_message(error: BaseException) -> str:
    """将 error 转成安全日志字符串；已知错误保留脱敏描述，系统异常仅附错误码，其余仅返回类型。"""
    from .youku.client import PlaybackError
    from .runtime import TaskCancelled
    from .storage import RecordWriteError
    if isinstance(error,(RuntimeError,TimeoutError)) and str(error).startswith(("FFmpeg 退出码", "媒体处理超时")):
        return str(error)
    if isinstance(error, (PlaybackError, TaskCancelled, RecordWriteError, ProtocolError)):
        message = str(error)
        if not re.search(r"https?://|cookie|encryptR|copyright_key|ckey=", message, re.I):
            return message
    if isinstance(error, OSError):
        return f"{type(error).__name__}（errno={error.errno}，winerror={getattr(error, 'winerror', None)}）"
    return type(error).__name__
