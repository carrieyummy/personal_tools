"""在线程和媒体子进程之间传递批次取消信号，不强制终止持有 SDK 的线程。"""

from threading import Event


class TaskCancelled(Exception):
    """表示任务响应批次取消；保留现场，由调度线程统一登记为中断。"""


def check_cancel(cancel_event: Event | None) -> None:
    """检查可选的线程取消信号 cancel_event；已取消时抛出 TaskCancelled，否则无返回值。"""
    if cancel_event is not None and cancel_event.is_set():
        raise TaskCancelled("批次已中断")
