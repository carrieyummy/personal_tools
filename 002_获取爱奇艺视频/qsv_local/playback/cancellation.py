"""管理链接和专辑级取消信号，不影响其他批次任务。"""

from threading import Event, Lock
from uuid import uuid4

from .coordination import SegmentCancellation


class CancellationRegistry:
    """保存批次内的稳定链接标识、专辑关联和局部取消状态。"""

    def __init__(self, options: list[dict], indexes: list[int] | None, batch_cancel: Event, interval: float):
        """根据 options、可选 indexes 和 batch_cancel 建立清单快照；interval 为轮询秒数。"""
        self.lock, self.batch_cancel, self.interval = Lock(), batch_cancel, interval
        chosen = set(range(len(options))) if indexes is None else set(indexes)
        self.links = {row["index"]: {"id": uuid4().hex, "index": row["index"], "url": row["url"],
                                    "status": "pending", "album_id": "", "title": "", "event": Event()}
                      for row in options if row["index"] in chosen}
        self.albums = {}

    def begin(self, index: int):
        """标记 index 对应链接正在解析；返回组合批次和链接事件的取消信号。"""
        with self.lock:
            row = self.links[index]
            if row["status"] == "pending":
                row["status"] = "discovering"
            return SegmentCancellationView(self.batch_cancel, row["event"], self.interval)

    def resolved(self, index: int, video: dict):
        """关联 index 与 video 的官方专辑；返回组合批次、链接和专辑事件的信号。"""
        with self.lock:
            row = self.links[index]
            album = str(video["album_id"])
            event = self.albums.setdefault(album, Event())
            row.update(album_id=album, title=video["title"])
            return SegmentCancellationView(SegmentCancellationView(self.batch_cancel, row["event"], self.interval), event, self.interval)

    def finish(self, index: int, status: str) -> None:
        """将 index 的目录解析标记为 status；已取消时保留取消状态。"""
        with self.lock:
            row = self.links[index]
            row["status"] = "cancelled" if row["event"].is_set() or (row["album_id"] and self.albums[row["album_id"]].is_set()) else status

    def cancel(self, kind: str, identifier: str) -> bool:
        """按 kind（link 或 album）与 identifier 请求取消；找到目标返回真。"""
        with self.lock:
            if kind == "link":
                row = next((r for r in self.links.values() if r["id"] == identifier), None)
                if row is None or row["status"] not in ("pending", "discovering"):
                    return False
                row["event"].set()
                row["status"] = "cancelled"
                return True
            if kind == "album" and identifier in self.albums:
                self.albums[identifier].set()
                for row in self.links.values():
                    if row["album_id"] == identifier:
                        row["status"] = "cancelled"
                return True
            return False

    def snapshot(self) -> tuple[list[dict], list[str]]:
        """返回不含事件对象的链接状态，以及已取消的专辑 ID。"""
        with self.lock:
            return ([{key: value for key, value in row.items() if key != "event"} for row in self.links.values()],
                    [key for key, event in self.albums.items() if event.is_set()])


class SegmentCancellationView(SegmentCancellation):
    """复用组合信号的轮询实现，将局部事件绑定到外部管理的事件。"""

    def __init__(self, shared, local: Event, interval: float):
        """绑定 shared 信号、local 事件与 interval 轮询秒数。"""
        super().__init__(shared, interval)
        self.local = local
