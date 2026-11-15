"""按客户端完成记录调度有界批次，验证来源和成品后安全发布并登记。"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Event, Lock, Semaphore
import time
from uuid import uuid4

from .hls import discover, discover_all, read_segments
from .configuration import series_directory_name
from .journal import Journal
from .native import NativeFingerprintMismatch, dll_digest, latest_dll
from .native_compat import profile_lock, read_profile, update_digest
from .process import probe_media, read_progress, run_process
from .runtime import TaskCancelled, check_cancel
from .storage import atomic_json, fingerprint, output_lock, publish
from .workflow import extract


class PendingDownload(Exception):
    """表示客户端下载未完成、状态不明确或缓存结构不在已验证范围。"""


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


def source_state(config: dict, task: dict, cancel: Event) -> dict:
    """检查任务 task 的下载状态和全部来源内容；config 控制路径与缓冲，cancel 可取消；返回可比较指纹。"""
    try:
        current = discover(config, task["video_id"])
    except (ValueError, KeyError, OSError) as error:
        raise PendingDownload(str(error)) from error
    if current != task:
        raise ValueError("客户端记录发生变化")
    try:
        segments, duration = read_segments(task["folder"])
    except (ValueError, KeyError, OSError) as error:
        raise PendingDownload(str(error)) from error
    if sum(s["media_bytes"] for s in segments) != task["media_bytes"]:
        raise PendingDownload("分片总长度与客户端下载记录不一致")
    values = []
    for s in segments:
        fp = fingerprint(s["path"], config["copy_buffer_bytes"], force=True, cancel_event=cancel)
        values.append({"path": str(s["path"]), "size": fp["size_bytes"], "sha256": fp["sha256"]})
    playlist = fingerprint(task["folder"] / ".m3u8", config["copy_buffer_bytes"], force=True, cancel_event=cancel)
    if discover(config, task["video_id"]) != task:
        raise ValueError("指纹计算期间客户端记录发生变化")
    return {"segments": segments, "duration": duration, "sources": values, "playlist_sha256": playlist["sha256"]}


def same_source(left: dict, right: dict) -> bool:
    """比较来源状态 left 和 right 的清单、时长及所有分片内容；返回是否一致。"""
    return (left["duration"] == right["duration"] and left["playlist_sha256"] == right["playlist_sha256"]
            and left["sources"] == right["sources"])


def index_path(config: dict, task: dict) -> Path:
    """按 config 的输出根目录和命名模板计算 task 的索引路径；task 须含 series/series_id，越界时报错。"""
    name = series_directory_name(config, task["series"], task["series_id"])
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


def reusable(config: dict, task: dict, state: dict, index: dict, cancel: Event) -> Path | None:
    """检查 index 中 task 已验证成品与 state 来源；按 config 重算成品摘要，返回可复用路径或 None。"""
    for item in reversed(index["files"]):
        if (item.get("source_type", "download") != "download"
                or item.get("video_id") != task["video_id"] or item.get("fid") != task["fid"]
                or item.get("sources") != state["sources"] or item.get("playlist_sha256") != state["playlist_sha256"]
                or item.get("verification", {}).get("exit_code") != 0):
            continue
        path = (index_path(config, task).parent / item["file"]).resolve()
        if not path.is_relative_to(index_path(config, task).parent) or not path.is_file():
            continue
        fp = fingerprint(path, config["copy_buffer_bytes"], force=True, cancel_event=cancel)
        if fp["sha256"] == item.get("sha256"):
            return path
    return None


def legacy_candidate(config: dict, task: dict, state: dict, cancel: Event) -> Path | None:
    """验证旧单集报告与 task、state 的完整来源和成品摘要；返回原成品路径或 None。"""
    for report_path in sorted(config["work_dir"].glob("*/report.json")):
        check_cancel(cancel)
        try:
            old = json.loads(report_path.read_text(encoding="utf-8"))
            if (old.get("status") != "verified" or old.get("video_id") != task["video_id"]
                    or old.get("resource") != task["fid"] or old.get("format_id") != task["format_id"]
                    or old.get("sources") != state["sources"] or old.get("segments") != len(state["segments"])
                    or abs(old.get("playlist_duration_seconds", 0) - state["duration"]) > config["duration_tolerance_seconds"]
                    or old.get("verification", {}).get("exit_code") != 0):
                continue
            path = Path(old["output"]).resolve()
            if not path.is_relative_to(config["output_path"]) or not path.is_file():
                continue
            fp = fingerprint(path, config["copy_buffer_bytes"], force=True, cancel_event=cancel)
            if fp["sha256"] == old.get("output_sha256") and fp["size_bytes"] == old.get("output_bytes"):
                return path
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


class Batch:
    """独占输出根目录的批次调度器；每视频独立现场，阶段槽限制 DLL、IO 和验证设备。"""

    def __init__(self, config: dict, journal: Journal):
        """按 config 和 journal 初始化可取消的资源槽；不启动任务。"""
        self.config, self.journal, self.cancel = config, journal, Event()
        self.interval = config["runtime"]["cancel_poll_interval_seconds"]
        self.prepare, self.mux, self.check, self.publish_slot = (Semaphore(1) for _ in range(4))
        self.devices = Queue()
        for device in ("gpu", "cpu"):
            if config["decode_verification"][device]:
                self.devices.put(device)
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

    def mux_candidate(self, source: Path, work: Path, entry: dict) -> Path:
        """将 source 无损封装到 work；entry 接收进度，校验音视频轨后返回候选文件路径。"""
        config = self.config
        self.notify(entry, "muxing", detail="无损封装 MP4")
        with self.resource(entry, "mux", self.mux):
            source_media = self.media(source, work, "source_media")
            candidate = work / "candidate.partial.mp4"
            command = [str(config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning", "-n", "-xerror",
                       "-i", str(source), "-map", "0:v:0", "-map", "0:a", "-c", "copy", "-movflags", "+faststart", str(candidate)]
            run_process(command, work / "mux.log", config, cancel_event=self.cancel)
            out_media = self.media(candidate, work, "output_media")
            if (source_media["video_codec"] != out_media["video_codec"]
                    or source_media["audio_codecs"] != out_media["audio_codecs"]):
                raise ValueError("封装前后视频或音轨不一致")
            return candidate

    def ensure_native_compatibility(self, tasks: list[dict], *, prepare_held: bool = False) -> None:
        """检测最新 DLL；变化时用 tasks 首集隔离验收，通过后更新配置；prepare_held 表示调用者已持有还原槽。"""
        config, cancel = self.config, self.cancel
        profile_path, profile = config["native_profile"], config["native"]
        path = latest_dll(profile["dll_root"], profile["dll_name"])
        if dll_digest(path) == profile["dll_sha256"]:
            return
        with profile_lock(profile_path, cancel, self.interval):
            raw, current = read_profile(profile_path)
            expected_other = {key: value for key, value in profile.items() if key != "dll_sha256"}
            if {key: value for key, value in current.items() if key != "dll_sha256"} != expected_other:
                raise RuntimeError("DLL 配置在运行期间被修改，请重新启动")
            path = latest_dll(current["dll_root"], current["dll_name"])
            digest = dll_digest(path)
            if digest == current["dll_sha256"]:
                config["native"] = current
                return
            if current["dll_sha256"] != profile["dll_sha256"]:
                raise RuntimeError("DLL 配置指纹在运行期间被修改，请重新启动")
            if not tasks:
                raise RuntimeError("DLL 指纹已变化，但没有已完成视频可用于自动验收")
            task = tasks[0]
            work = config["work_dir"] / self.journal.run["id"] / f"native_{uuid4().hex}"
            work.mkdir(parents=True, exist_ok=False)
            entry = self.journal.add({"video_id": f"native_{digest}", "title": "DLL 兼容性验证"})
            try:
                self.notify(entry, "checking_source", detail="检查验收样本来源", work_dir=str(work))
                state = source_state(config, task, cancel)
                probe_profile = deepcopy(current)
                probe_profile["dll_root"] = str(probe_profile["dll_root"])
                probe_profile["dll_sha256"] = digest
                request = {"config": {"native": probe_profile, "copy_buffer_bytes": config["copy_buffer_bytes"]},
                           "dll_path": str(path), "folder": str(task["folder"]),
                           "segments": [{**segment, "path": str(segment["path"])} for segment in state["segments"]],
                           "target": str(work / "restored.ts"), "result": str(work / "probe_result.json")}
                request_path = work / "probe_request.json"
                atomic_json(request_path, request)
                self.notify(entry, "extracting", detail="在隔离子进程验收新 DLL")
                command = [sys.executable, "-X", "faulthandler", "-c", "from local.native_probe import main; main()", str(request_path)]
                with nullcontext() if prepare_held else self.resource(entry, "extract", self.prepare):
                    run_process(command, work / "native_probe.log", config, cancel_event=cancel,
                                cwd=Path(__file__).resolve().parent.parent, label="DLL 验证子进程")
                result = json.loads((work / "probe_result.json").read_text(encoding="utf-8"))
                if result["sources"] != state["sources"]:
                    raise ValueError("DLL 验收期间来源分片发生变化")
                candidate = self.mux_candidate(work / "restored.ts", work, entry)
                self.media(candidate, work, "candidate_media")
                self.verify(candidate, work, state["duration"], entry)
                self.notify(entry, "checking", detail="复核来源、DLL 和配置")
                if not same_source(state, source_state(config, task, cancel)):
                    raise ValueError("DLL 验收期间来源文件或清单发生变化")
                if latest_dll(current["dll_root"], current["dll_name"]) != path or dll_digest(path) != digest:
                    raise RuntimeError("DLL 验收期间客户端版本或文件发生变化")
                update_digest(profile_path, raw, current["dll_sha256"], digest)
                current["dll_sha256"] = digest
                config["native"] = current
                self.journal.update(entry, status="completed", detail=f"DLL 验收通过，已更新指纹：{path}")
                if not config["keep_intermediates"]:
                    for intermediate in (work / "restored.ts", candidate):
                        try:
                            intermediate.unlink(missing_ok=True)
                        except OSError as error:
                            self.journal.update(entry, detail=f"DLL 验收通过，清理中间文件失败：{error}")
            except BaseException as error:
                status = "interrupted" if isinstance(error, (TaskCancelled, KeyboardInterrupt)) else "failed"
                self.journal.update(entry, status=status, detail=f"DLL 自动验收失败：{error}")
                raise

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
                    device = self.devices.get(timeout=self.interval)
                    break
                except Empty:
                    continue
            acquired = time.monotonic()
            self.journal.update(entry, detail=f"完整解码 {device.upper()}", device=device)
            config = self.config
            progress = work / "decode.progress"
            command = [str(config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning", "-n", "-xerror", "-err_detect", "explode"]
            if device == "gpu":
                command += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            command += ["-i", str(candidate), "-map", "0:v:0", "-map", "0:a", "-fps_mode", "passthrough",
                        "-progress", str(progress), "-f", "null", "-"]
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
            return {"device": device, "frames": frames, "duration_seconds": seconds, "exit_code": 0}
        finally:
            ended = time.monotonic()
            if device is not None:
                self.devices.put(device)
            self.journal.record_resource(entry, f"verification_{device}" if device else "verification",
                                         (acquired if acquired is not None else ended) - started,
                                         ended - acquired if acquired is not None else 0.0)

    def job(self, task: dict, entry: dict) -> dict:
        """独立处理 task，向 entry 汇报；成功返回结果字典，异常由调度器统一记录。"""
        config, cancel = self.config, self.cancel
        work = config["work_dir"] / self.journal.run["id"] / uuid4().hex
        work.mkdir(parents=True, exist_ok=False)
        report = {"video_id": task["video_id"], "work_dir": str(work), "status": "running"}
        try:
            self.notify(entry, "checking_source", detail="检查客户端下载状态和分片摘要", work_dir=str(work))
            with self.resource(entry, "source_check", self.check):
                state = source_state(config, task, cancel)
                with self.index_lock:
                    index = load_index(index_path(config, task))
                existing = reusable(config, task, state, index, cancel)
                if existing:
                    latest = source_state(config, task, cancel)
                    if not same_source(state, latest):
                        raise ValueError("复用前源内容发生变化")
                    report.update(status="reused", output=str(existing))
                    return report
                legacy = legacy_candidate(config, task, state, cancel)
            if legacy:
                candidate = legacy
                self.notify(entry, "migrating", detail="核对旧版成品并完整解码")
            else:
                self.notify(entry, "extracting", detail="还原 HVN 分片")
                with self.resource(entry, "extract", self.prepare):
                    while True:
                        check_cancel(cancel)
                        self.ensure_native_compatibility([task], prepare_held=True)
                        try:
                            sources = extract(config, state["segments"], task["folder"], work / "restored.ts", cancel_event=cancel,
                                              notify=lambda p: self.journal.update(entry, progress=p))
                            break
                        except NativeFingerprintMismatch:
                            # 加载前再次发生版本切换；尚未创建 TS，重新走隔离验收。
                            continue
                    if sources != state["sources"]:
                        raise ValueError("还原时源分片发生变化")
                candidate = self.mux_candidate(work / "restored.ts", work, entry)
            media = self.media(candidate, work, "candidate_media")
            proof = self.verify(candidate, work, state["duration"], entry)
            self.notify(entry, "checking", detail="复核全部来源、清单和成品摘要")
            with self.resource(entry, "source_check", self.check):
                if not same_source(state, source_state(config, task, cancel)):
                    raise ValueError("处理期间源文件或清单发生变化")
                output_fp = fingerprint(candidate, config["copy_buffer_bytes"], force=True, cancel_event=cancel)
            self.notify(entry, "publishing", detail="复制、校验并登记成品")
            with self.resource(entry, "publish", self.publish_slot):
                with self.resource(entry, "source_check", self.check):
                    if not same_source(state, source_state(config, task, cancel)):
                        raise ValueError("发布前源文件或下载状态发生变化")
                path = index_path(config, task)
                with self.index_lock:
                    index = load_index(path)
                existing = reusable(config, task, state, index, cancel)
                if existing:
                    report.update(status="reused", output=str(existing))
                    return report
                path.parent.mkdir(parents=True, exist_ok=True)
                target = path.parent / (task["title"] + ".mp4")
                if target.exists():
                    suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
                    target = path.parent / f"{task['title']}_{suffix}_{uuid4().hex[:8]}.mp4"
                published = False
                try:
                    def check_before_publish() -> None:
                        """目标盘复制后重新校验来源和下载状态；变更时阻止重命名，无返回值。"""
                        with self.resource(entry, "source_check", self.check):
                            if not same_source(state, source_state(config, task, cancel)):
                                raise ValueError("复制成品期间源文件或下载状态发生变化")
                    publish(candidate, target, output_fp["sha256"], config["copy_buffer_bytes"],
                            cancel_event=cancel, before_publish=check_before_publish)
                    published = True
                    index["files"].append({"source_type": "download", "video_id": task["video_id"], "fid": task["fid"], "file": target.name,
                                           "sources": state["sources"], "playlist_sha256": state["playlist_sha256"],
                                           "sha256": output_fp["sha256"], "bytes": output_fp["size_bytes"],
                                           "verification": proof, "media": media, "created": datetime.now().astimezone().isoformat(),
                                           "work_dir": str(work), "legacy": bool(legacy)})
                    with self.index_lock:
                        atomic_json(path, index)
                except BaseException:
                    if published:
                        target.unlink(missing_ok=True)
                    raise
            report.update(status="completed", output=str(target), verification=proof, media=media,
                          sha256=output_fp["sha256"], legacy=bool(legacy))
            if not config["keep_intermediates"]:
                for path in (work / "restored.ts", work / "candidate.partial.mp4"):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as error:
                        report["cleanup_error"] = str(error)
            return report
        except BaseException as error:
            status = "interrupted" if isinstance(error, TaskCancelled) else "skipped" if isinstance(error, PendingDownload) else "failed"
            report.update(status=status, error=str(error))
            raise
        finally:
            atomic_json(work / "report.json", report)

    def run(self, tasks: list[dict]) -> dict:
        """对 tasks 执行有界流水线；逐项失败继续，取消回收任务；返回状态计数。"""
        pending = iter((task, self.journal.add(task)) for task in tasks)
        active = {}
        summary = {"completed": 0, "reused": 0, "failed": 0, "interrupted": 0, "skipped": 0}
        self.summary = summary
        executor = ThreadPoolExecutor(max_workers=self.config["max_in_flight"], thread_name_prefix="tencent")
        try:
            exhausted = False
            while active or not exhausted:
                while not exhausted and len(active) < self.config["max_in_flight"]:
                    try:
                        task, entry = next(pending)
                    except StopIteration:
                        exhausted = True
                        break
                    active[executor.submit(self.job, task, entry)] = entry
                if not active:
                    continue
                done, _ = wait(active, timeout=self.interval, return_when=FIRST_COMPLETED)
                for future in done:
                    entry = active.pop(future)
                    try:
                        result = future.result()
                        status = result["status"]
                        self.journal.update(entry, progress=100.0, detail=result.get("output", ""), **result)
                    except Exception as error:
                        status = "interrupted" if isinstance(error, TaskCancelled) else "skipped" if isinstance(error, PendingDownload) else "failed"
                        self.journal.update(entry, status=status, detail=f"{type(error).__name__}: {error}")
                    summary[status] += 1
        except KeyboardInterrupt:
            self.cancel.set()
            executor.shutdown(wait=True, cancel_futures=True)
            for future, entry in active.items():
                if future.cancelled():
                    status = "interrupted"
                else:
                    try:
                        status = future.result()["status"]
                    except Exception:
                        status = "interrupted"
                summary[status] += 1
                self.journal.update(entry, status=status, detail="批次中断")
            for task, entry in pending:
                summary["interrupted"] += 1
                self.journal.update(entry, status="interrupted", detail="中断前未派发")
            raise
        finally:
            self.cancel.set()
            executor.shutdown(wait=True, cancel_futures=True)
        return summary


def run_batch(config: dict) -> dict:
    """按 config 获取输出锁、自动验收变化的 DLL 并处理批次；返回汇总，故障保留记录并向上传播。"""
    with output_lock(config["output_path"]):
        tasks, skipped = discover_all(config)
        journal = Journal(config)
        print(f"实时日志：{journal.url}", flush=True)
        summary = {"total": len(tasks) + len(skipped), "completed": 0, "reused": 0, "failed": 0,
                   "interrupted": 0, "skipped": len(skipped)}
        status = "failed"
        batch = Batch(config, journal)
        try:
            try:
                batch.ensure_native_compatibility(tasks)
            except Exception:
                summary["native_validation"] = "failed"
                raise
            for item in skipped:
                entry = journal.add(item)
                journal.update(entry, status="skipped", detail=item["detail"])
            for key, count in batch.run(tasks).items():
                summary[key] += count
            status = "warning" if summary["failed"] else "completed"
            return summary
        except KeyboardInterrupt:
            status = "interrupted"
            for key, count in getattr(batch, "summary", {}).items():
                summary[key] += count
            raise
        finally:
            print(f"离线报告：{journal.close(status, summary)}", flush=True)
