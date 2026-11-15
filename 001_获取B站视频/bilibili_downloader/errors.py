"""定义各业务模块共用的可解释运行错误。"""

from threading import Event
from contextvars import ContextVar


current_cancel_event: ContextVar[Event | None] = ContextVar('bilibili_cancel_event', default=None)


class ToolError(RuntimeError):
    """表示可直接向用户解释的配置、登录、下载、元数据或封装错误。"""


class DownloadCancelled(KeyboardInterrupt):
    """表示整次运行被取消；继承 KeyboardInterrupt，避免被普通失败重试或降级处理吞掉。"""


def check_cancel(event: Event | None = None) -> None:
    """检查可选 event（None 使用当前运行上下文）；设置时抛出 DownloadCancelled，否则无返回值。"""
    event = event if event is not None else current_cancel_event.get()
    if event is not None and event.is_set():
        raise DownloadCancelled('用户已取消下载')
