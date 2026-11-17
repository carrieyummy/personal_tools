"""优酷公共阶段槽、完整解码与成品索引；不依赖其他平台缓存。"""
from contextlib import contextmanager
import json
from pathlib import Path
from queue import PriorityQueue, Empty
from threading import Event, Lock, Semaphore
import time
from .configuration import series_directory_name
from .journal import Journal
from .process import probe_media, run_process, read_progress
from .runtime import check_cancel, TaskCancelled
from .storage import atomic_json, fingerprint, valid_digest

@contextmanager
def slot(semaphore: Semaphore, cancel: Event, interval: float):
    """等待资源槽 semaphore；cancel 可取消，interval 是轮询秒数；退出时释放槽。"""
    while not semaphore.acquire(timeout=interval):
        check_cancel(cancel)
    try:
        check_cancel(cancel)
        yield
    finally:
        semaphore.release()

def index_path(config: dict, task: dict) -> Path:
    """按 config 的输出根目录和命名模板计算 task 的索引路径；task 须含 series/series_id，越界时报错。"""
    name = series_directory_name(config, task["series"], task["series_id"], task["release_date"])
    folder = (config["output_path"] / name).resolve()
    if not folder.is_relative_to(config["output_path"]) or folder == config["output_path"]:
        raise ValueError("剧集输出目录越界")
    return folder / "video_info.json"

def load_index(path: Path) -> dict:
    """读取索引 path；不存在时返回新索引，损坏时拒绝修改。"""
    result = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "files": []}
    if not isinstance(result, dict) or result.get("version") != 1 or not isinstance(result.get("files"), list):
        raise ValueError(f"成品索引无效：{path}")
    return result

def check_record(config: dict, location: Path, path: Path, item: dict, cancel: Event, lock: Lock) -> bool:
    """按配置核对已登记成品 path；location 为索引、item 为记录，cancel 可取消，lock 保护索引；返回是否匹配。

    快速检查仅信任已校验的大小和修改时间；旧记录或属性变化时重算摘要，成功后原子补充时间戳。
    """
    if not valid_digest(item.get("sha256")):
        return False
    cached = {"size_bytes": item.get("bytes"), "mtime_ns": item.get("mtime_ns"), "sha256": item["sha256"]}
    actual = fingerprint(path, config["copy_buffer_bytes"], cached, force=config["reuse_check"] == "full", cancel_event=cancel)
    if actual["sha256"] != item["sha256"]:
        return False
    if (item.get("bytes"), item.get("mtime_ns")) != (actual["size_bytes"], actual["mtime_ns"]):
        with lock:
            index = load_index(location)
            for current in index["files"]:
                if current == item:
                    current.update(bytes=actual["size_bytes"], mtime_ns=actual["mtime_ns"])
                    atomic_json(location, index, config["storage"])
                    break
                if current == {**item, "bytes": actual["size_bytes"], "mtime_ns": actual["mtime_ns"]}:
                    break  # 同一旧记录可能已被另一个任务完成升级。
            else:
                return False
    return True

class Batch:
    """管理可取消的公共资源槽和 CPU/GPU 验证池。"""

    def __init__(self, config: dict, journal: Journal):
        """按 config 和 journal 初始化可取消的资源槽；不启动任务。"""
        self.config, self.journal, self.cancel = config, journal, Event()
        self.interval = config["runtime"]["cancel_poll_interval_seconds"]
        self.prepare, self.mux, self.check, self.publish_slot = (Semaphore(1) for _ in range(4))
        self.devices = PriorityQueue()
        for device in ("gpu", "cpu"):
            if config["decode_verification"][device]:
                self.devices.put((0 if device == "gpu" else 1, device))
        self.index_lock = Lock()

    def notify(self, entry: dict, stage: str, **fields) -> None:
        """将 entry 的 stage、fields 写入日志；所有阶段开始时标记运行中，无返回值。"""
        self.journal.update(entry, stage, status="running", **fields)

    @contextmanager
    def resource(self, entry: dict, name: str, semaphore: Semaphore):
        """为 entry 获取 name 对应的 semaphore；记录等待及持有秒数，退出时释放。"""
        started = time.monotonic()
        acquired = None
        try:
            with slot(semaphore, self.cancel, self.interval):
                acquired = time.monotonic()
                yield
        finally:
            ended = time.monotonic()
            self.journal.record_resource(entry, name, (acquired if acquired is not None else ended) - started,
                                         ended - acquired if acquired is not None else 0.0)

    def media(self, path: Path, work: Path, label: str) -> dict:
        """用当前配置探测 path 的音视频信息，日志位于 work/label；返回媒体字典。"""
        media = probe_media(self.config, path, work / f"{label}.log", cancel_event=self.cancel)
        if media["video_codec"] is None or not media["audio_codecs"]:
            raise ValueError("媒体缺少可识别的视频或音轨")
        return media

    def verify(self, candidate: Path, work: Path, duration: float, entry: dict) -> dict:
        """在可用 CPU/GPU 槽完整验证 candidate 的全部音轨；work 保存日志，duration 为清单时长；返回验证证明。"""
        self.notify(entry, "verifying", detail="等待完整解码设备")
        started = time.monotonic()
        device = None
        acquired = None
        try:
            while True:
                check_cancel(self.cancel)
                try:
                    _, device = self.devices.get(timeout=self.interval)
                    break
                except Empty:
                    continue
            acquired = time.monotonic()
            self.journal.update(entry, detail=f"完整解码 {device.upper()}", device=device)
            config = self.config
            progress = work / "decode.progress"
            audio_hashes = work / "audio.framehash"
            command = [str(config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning", "-n", "-xerror", "-err_detect", "explode"]
            if device == "gpu":
                command += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            command += ["-i", str(candidate), "-map", "0:v:0", "-map", "0:a", "-fps_mode", "passthrough",
                        "-progress", str(progress), "-f", "null", "-", "-map", "0:a", "-f", "framehash", str(audio_hashes)]
            def update(values: dict) -> None:
                """按 FFmpeg values 更新进度；无返回值。"""
                seconds = int(values.get("out_time_us", 0)) / 1_000_000
                self.journal.update(entry, progress=min(99.9, seconds * 100 / duration))
            run_process(command, work / "decode.log", config, update, progress, cancel_event=self.cancel)
            values = read_progress(progress)
            frames = int(values.get("frame", 0))
            seconds = int(values["out_time_us"]) / 1_000_000
            if (values.get("progress") != "end" or frames <= 0 or int(values.get("dup_frames", 0))
                    or int(values.get("drop_frames", 0)) or abs(seconds - duration) > config["duration_tolerance_seconds"]):
                raise ValueError("完整解码帧数、时间或终态不符合要求")
            from collections import Counter
            from fractions import Fraction
            audio_frames, bases, starts, ends = Counter(), {}, {}, {}
            for line in audio_hashes.read_text(encoding="utf-8").splitlines():
                if line.startswith("#tb "):
                    track, base = line[4:].split(":",1)
                    bases[int(track)] = Fraction(base.strip())
                elif line and not line.startswith("#"):
                    fields = [v.strip() for v in line.split(",")]
                    track, pts, length, size = int(fields[0]), int(fields[2]), int(fields[3]), int(fields[4])
                    if size <= 0 or length <= 0 or track not in bases:
                        raise ValueError("音频完整解码未产出有效帧")
                    audio_frames[track] += 1
                    starts[track] = min(starts.get(track,pts),pts)
                    ends[track] = max(ends.get(track,pts+length),pts+length)
            audio_durations = {track:float((ends[track]-starts[track])*bases[track]) for track in audio_frames}
            if not audio_frames or any(abs(value-duration)>config["duration_tolerance_seconds"] for value in audio_durations.values()):
                raise ValueError("音轨解码时长与完整正片不匹配")
            return {"device": device, "frames": frames, "duration_seconds": seconds,
                    "audio_frames":dict(audio_frames), "audio_duration_seconds":audio_durations,"exit_code": 0}
        finally:
            ended = time.monotonic()
            if device is not None:
                self.devices.put((0 if device == "gpu" else 1, device))
            self.journal.record_resource(entry, f"verification_{device}" if device else "verification",
                                         (acquired if acquired is not None else ended) - started,
                                         ended - acquired if acquired is not None else 0.0)
