"""链接批次的取消信号和独占阶段槽，不保存播放票据。"""

from contextlib import contextmanager
from copy import copy
from threading import Event, Lock
import time

from ..runtime import check_cancel


class SegmentCancellation:
    """组合单集停止与批次取消；用于回收当前集的并发分片，不影响其他集。"""

    def __init__(self, shared: Event, interval: float):
        """保存批次事件 shared 和轮询秒数 interval，创建单集停止事件；无返回值。"""
        self.shared, self.interval, self.local = shared, interval, Event()

    def is_set(self) -> bool:
        """任一停止事件已触发时返回真；无参数。"""
        return self.shared.is_set() or self.local.is_set()

    def set(self) -> None:
        """停止当前集的分片工作；不设置批次事件，无返回值。"""
        self.local.set()

    def wait(self, timeout: float) -> bool:
        """等待最多 timeout 秒，任一事件触发时返回真，否则返回假。"""
        deadline = time.monotonic() + timeout
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.local.wait(min(self.interval, remaining))
        return True


class StageControls:
    """限制 config 指定的设备和独占处理阶段；cancel 为批次协作取消信号。"""

    def __init__(self, config: dict, cancel: Event | None = None):
        """按 config 创建设备和阶段槽；cancel 为可选共享事件，无返回值。"""
        self.cancel = cancel if cancel is not None else Event()
        self.interval = config["runtime"]["cancel_poll_interval_seconds"]
        self.slots = {name: Lock() for name in ("sdk", "source", "mux", "check", "publish")}
        self.keys_lock = Lock()
        self.keys = {}
        if (not isinstance(config.get("decode_verification"), dict)
                or any(type(config["decode_verification"].get(name)) is not bool for name in ("gpu", "cpu"))):
            raise ValueError("decode_verification 必须包含 cpu、gpu 两个布尔开关")
        self.devices = {name: Lock() for name in ("gpu", "cpu") if config["decode_verification"][name]}
        if not self.devices:
            raise ValueError("decode_verification.cpu/gpu 至少开启一个")

    def for_task(self, stopped):
        """共享当前独占槽并使用 stopped 单任务取消信号；返回独立控制视图。"""
        result = copy(self)
        result.cancel = stopped
        return result

    @contextmanager
    def slot(self, name: str):
        """等待名为 name 的独占阶段；响应取消并在退出时释放，无返回值。"""
        lock = self.slots[name]
        while not lock.acquire(timeout=self.interval):
            check_cancel(self.cancel)
        try:
            check_cancel(self.cancel)
            yield
        finally:
            lock.release()

    @contextmanager
    def identity(self, key: str):
        """独占来源 key 对应的工作目录直至任务完成；响应取消并释放，无返回值。"""
        with self.keys_lock:
            lock = self.keys.setdefault(key, Lock())
        while not lock.acquire(timeout=self.interval):
            check_cancel(self.cancel)
        try:
            check_cancel(self.cancel)
            yield
        finally:
            lock.release()

    @contextmanager
    def device(self):
        """获取空闲解码设备，优先 GPU；响应取消，返回设备名称并在退出时释放。"""
        selected = None
        while selected is None:
            check_cancel(self.cancel)
            for name, lock in self.devices.items():
                if lock.acquire(blocking=False):
                    selected = (name, lock)
                    break
            if selected is None:
                self.cancel.wait(self.interval)
        try:
            check_cancel(self.cancel)
            yield selected[0]
        finally:
            selected[1].release()
