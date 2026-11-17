"""将腾讯 URL 来源接入公共调度、完整解码、进度记录和不覆盖发布；保留每个源版本的断点。"""

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
import re
import shutil
from threading import Event, Semaphore
from uuid import uuid4

from ..batch import Batch, PendingDownload, check_record, index_path, load_index
from ..journal import Journal
from ..process import run_process
from ..runtime import TaskCancelled, check_cancel
from ..storage import atomic_json, fingerprint, output_lock, publish, timestamp
from .audio import AudioSelector
from .client import NativeClient
from .errors import AuthenticationError, AuthenticationStopped, PlaybackError, SourceChanged
from .hls import download
from .source import Resolved, resolve
from .tasks import discover, requests
from .transport import safe_message


def safe_name(value: str) -> str:
    """将源站标题 value 转成无空白的 Windows 文件名片段；返回非空单层名称。"""
    result = re.sub(r'[\s<>:"/\\|?*\x00-\x1f]+', "_", value).strip(". _")
    if not result:
        raise PlaybackError("源站标题清理后为空")
    return result


def task_index(config: dict, task: dict) -> Path:
    """按 config 和 task 取得共用剧目录索引；仅清理目录标题，日志保留原站文字。"""
    return index_path(config, {**task, "series": safe_name(task["series"])})


def reusable(config: dict, task: dict, source: Resolved, cancel: Event, lock) -> Path | None:
    """按 config 检查 task/source 的 URL 成品；cancel 可取消，lock 保护索引，返回路径或 None。"""
    location = task_index(config, task)
    with lock:
        index = load_index(location)
    for item in reversed(index["files"]):
        if (item.get("source_type") != "url" or item.get("video_id") != task["video_id"]
                or item.get("source_identity") != source.identity or item.get("verification", {}).get("exit_code") != 0):
            continue
        path = (location.parent / item["file"]).resolve()
        if not path.is_relative_to(location.parent) or not path.is_file():
            continue
        if check_record(config, location, path, item, cancel, lock):
            return path
    return None


class UrlBatch(Batch):
    """复用缓存批次的资源槽、媒体探测和完整解码；URL 仅替换来源准备及来源身份检查。"""

    def __init__(self, config: dict, journal: Journal, *, client: NativeClient | None = None):
        """以 config/journal 创建 URL 批次；client 可由测试注入，None 时通过隔离进程只读客户端登录态。"""
        super().__init__(config, journal)
        self.client = NativeClient(config, self.cancel) if client is None else client
        self.authentication = getattr(self.client, "authentication", None)
        self.auth_stop = Event() if self.authentication is None else self.authentication.stopped
        if self.authentication is not None:
            self.authentication.cancel = self.cancel
        self.client.transport.auth_stop = self.auth_stop
        self.client.transport.cancel = self.cancel
        self.selector = AudioSelector(config["audio"])
        self.download_slots = Semaphore(config["download"]["threads"])

    def dispatch_stop_reason(self) -> str | None:
        """认证恢复耗尽后返回未派发任务的跳过原因；否则返回 None，允许流水线继续。"""
        return "因认证恢复失败未执行，请在客户端重新登录后重新运行" if self.auth_stop.is_set() else None

    def mux_manifests(self, manifests: list[Path], work: Path, entry: dict) -> Path:
        """将已完整下载的 manifests 无损封装到 work；entry 接收阶段；返回候选 MP4 路径。"""
        self.notify(entry, "muxing", detail="无损封装视频和所选原始音轨")
        with self.resource(entry, "mux", self.mux):
            candidate = work / f"candidate_{uuid4().hex}.partial.mp4"
            command = [str(self.config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning", "-n"]
            for manifest in manifests:
                command += ["-protocol_whitelist", "file,crypto,data", "-allowed_extensions", "ALL",
                            "-allowed_segment_extensions", "ALL", "-extension_picky", "0", "-i", str(manifest)]
            command += ["-map", "0:v:0", "-map", "0:a?"]
            for index in range(1, len(manifests)):
                command += ["-map", f"{index}:a"]
            command += ["-c", "copy", "-movflags", "+faststart", str(candidate)]
            run_process(command, work / f"mux_{uuid4().hex}.log", self.config, cancel_event=self.cancel)
            return candidate

    def check_media(self, media: dict, source: Resolved) -> None:
        """对照 source 检查 media 的尺寸、帧率和音轨；成功无返回值，不接受低规格成品。"""
        expected = source.description["format"]
        if (media["width"] != int(expected["width"]) or media["height"] != int(expected["height"])
                or media["fps"] is None
                or abs(media["fps"] - float(expected["vfps"])) > self.config["verification"]["fps_tolerance"]):
            raise PlaybackError("封装成品尺寸或帧率与所选画质不一致")
        if not media["audio_codecs"] or len(media["audio_codecs"]) < len(source.manifests) - 1:
            raise PlaybackError("封装成品缺少源音轨")
        for key in ("codec", "sample_rate"):
            value = source.audio.metadata.get(key)
            if value is not None and not any(str(track.get(key)) == str(value) for track in media["audio_streams"]):
                raise PlaybackError("封装成品音频属性与所选音质不符")

    def job(self, task: dict, entry: dict) -> dict:
        """同步处理 task 并更新 entry；返回终态报告，主要供单集调用。"""
        steps = self._job_steps(task, entry)
        while True:
            try:
                next(steps)
            except StopIteration as done:
                return done.value

    def _job_steps(self, task: dict, entry: dict):
        """分阶段处理 task 并更新 entry；让出下载完成的字节数或重试标记，结束时返回报告。"""
        config, cancel = self.config, self.cancel
        for attempt in range(config["download"]["refresh_attempts"] + 1):
            work, report = None, {"source_type": "url", "video_id": task["video_id"], "status": "running"}
            try:
                check_cancel(cancel)
                if self.auth_stop.is_set():
                    raise AuthenticationStopped("认证恢复失败，当前任务停止请求并保留现场")
                self.notify(entry, "resolving", detail="查询账号授权画质及音质")
                with self.resource(entry, "download", self.download_slots):
                    source = resolve(self.client, task, self.selector)
                    work = config["work_dir"] / "url" / task["series_id"] / task["video_id"] / source.identity
                    work.mkdir(parents=True, exist_ok=True)
                    report.update(source=source.description, source_identity=source.identity, work_dir=str(work))
                    self.journal.update(entry, source=source.description, source_identity=source.identity,
                                        detail=f"{source.description['format']['sname']} / {source.audio.name}；{source.audio.metadata.get('note', '')}")
                    existing = reusable(config, task, source, cancel, self.index_lock)
                    if existing:
                        report.update(status="reused", output=str(existing))
                        return report
                    atomic_json(work / "source.json", {"task": task, "source": source.description,
                                                        "identity": source.identity})
                    self.notify(entry, "downloading", detail=f"下载 {source.description['format']['sname']} / {source.audio.name}", work_dir=str(work))
                    def refresh() -> list:
                        """仅续签已选 source；返回同身份清单，变化时抛错，凭据或签名不落盘。"""
                        return resolve(self.client, task, self.selector, expected=source).manifests
                    manifests, evidence = download(self.client.transport, source.manifests, work, config, refresh,
                        lambda done, total: self.journal.update(entry, progress=done * 100 / total), cancel)
                    report["download"] = evidence
                size = sum((work / f"track_{track}" / f"{index:06d}.bin").stat().st_size
                           for track, playlist in enumerate(source.manifests)
                           for index, _ in enumerate(playlist.resources))
                yield "ready", size
                check_cancel(cancel)
                if self.auth_stop.is_set():
                    raise AuthenticationStopped("认证恢复失败，当前任务停止请求并保留现场")
                processing = work / "processing" / f"{self.journal.run['id']}_{uuid4().hex}"
                processing.mkdir(parents=True, exist_ok=False)
                report["processing_dir"] = str(processing)
                candidate = self.mux_manifests(manifests, processing, entry)
                media = self.media(candidate, processing, "media")
                self.check_media(media, source)
                verification = self.verify(candidate, processing, source.manifests[0].duration, entry)
                self.notify(entry, "checking_source", detail="复核画质、音质和来源版本")
                resolve(self.client, task, self.selector, expected=source)
                output = fingerprint(candidate, config["copy_buffer_bytes"], force=True, cancel_event=cancel)
                with self.resource(entry, "publish", self.publish_slot):
                    index_file = task_index(config, task)
                    index_file.parent.mkdir(parents=True, exist_ok=True)
                    name = config["naming"]["episode_template"].format(index=task["index"],
                        title=safe_name(task["title"])[:config["naming"]["max_title_length"]],
                        quality=safe_name(source.description["format"]["sname"]),
                        audio=safe_name(source.audio.name), video_id=task["video_id"])
                    target = index_file.parent / f"{name}.mp4"
                    if target.exists():
                        target = index_file.parent / f"{name}_{source.identity[:12]}_{uuid4().hex[:8]}.mp4"
                    published = False
                    try:
                        published_fp = publish(candidate, target, output["sha256"], config["copy_buffer_bytes"], cancel_event=cancel,
                                               before_publish=lambda: resolve(self.client, task, self.selector, expected=source))
                        published = True
                        record = {"source_type": "url", "video_id": task["video_id"], "file": target.name,
                            "source_identity": source.identity, "source": source.description,
                            "sha256": published_fp["sha256"], "bytes": published_fp["size_bytes"], "mtime_ns": published_fp["mtime_ns"],
                            "verification": verification, "media": media, "created": timestamp()}
                        with self.index_lock:
                            index = load_index(index_file)
                            index["files"].append(record)
                            atomic_json(index_file, index)
                    except BaseException:
                        if published:
                            target.unlink(missing_ok=True)
                        raise
                report.update(status="completed", output=str(target), media=media, verification=verification,
                              sha256=output["sha256"])
                if not config["keep_intermediates"]:
                    try:
                        for path in manifests:
                            target_dir = path.parent.resolve()
                            boundary = work.resolve()
                            if target_dir == boundary or not target_dir.is_relative_to(boundary):
                                raise OSError("拒绝清理超出本集工作目录的路径")
                            shutil.rmtree(target_dir)
                        candidate.unlink(missing_ok=True)
                        (work / "segments.json").unlink(missing_ok=True)
                    except OSError as error:
                        report["cleanup_error"] = safe_message(error)
                return report
            except AuthenticationStopped as error:
                report.update(status="skipped", error=safe_message(error))
                raise PendingDownload("因其他任务认证恢复失败未完成，已保留现场") from None
            except AuthenticationError as error:
                self.auth_stop.set()
                report.update(status="failed", error=safe_message(error))
                raise
            except SourceChanged as error:
                report.update(status="failed", error=safe_message(error))
                if attempt == config["download"]["refresh_attempts"]:
                    raise
                self.journal.update(entry, detail="来源版本变化，保留旧现场并重新解析")
                yield "retry", 0
            except BaseException as error:
                report.update(status="interrupted" if isinstance(error, (TaskCancelled, KeyboardInterrupt)) else "failed",
                              error=safe_message(error))
                raise
            finally:
                if self.authentication is not None:
                    report["authentication"] = self.authentication.diagnostics()
                    self.journal.update(entry, authentication=report["authentication"])
                if work is not None:
                    atomic_json(work / f"report_{self.journal.run['id']}.json", report)
        raise AssertionError("不可达的 URL 重试状态")

    @staticmethod
    def _advance(steps):
        """推进单集生成器 steps 一次；返回阶段标记及字节数，结束时返回报告标记和报告。"""
        try:
            return next(steps)
        except StopIteration as done:
            return "done", done.value

    def run(self, tasks: list[dict]) -> dict:
        """先登记全部 tasks，再独立调度下载和后处理；按分片大小限制等待队列，返回终态计数。"""
        pending = iter([(task, self.journal.add(task)) for task in tasks])
        retries, waiting, ready = deque(), deque(), deque()
        active_downloads, active_processing = {}, {}
        capacity = int(self.config["download"]["queue_capacity_gb"] * 1024**3)
        queued_bytes = 0
        summary = {key: 0 for key in ("completed", "reused", "failed", "interrupted", "skipped")}
        self.summary = summary
        exhausted = False
        downloads = ThreadPoolExecutor(max_workers=self.config["download"]["threads"], thread_name_prefix="tencent-download")
        processing = ThreadPoolExecutor(max_workers=self.config["max_in_flight"], thread_name_prefix="tencent-process")

        def finish(entry: dict, future) -> None:
            """处理 entry 的完成 future，记录终态或将生成器放入下一阶段，无返回值。"""
            try:
                kind, value = future.result()
                if kind == "done":
                    summary[value["status"]] += 1
                    self.journal.update(entry, progress=100.0, detail=value.get("output", ""), **value)
                elif kind == "ready":
                    self.notify(entry, "waiting_queue", detail="下载完成，等待队列容量")
                    waiting.append((entry, future.steps, value))
                elif kind == "retry":
                    retries.append((entry, future.steps))
                else:
                    raise RuntimeError("未知 URL 任务阶段")
            except Exception as error:
                status = "interrupted" if isinstance(error, TaskCancelled) else "skipped" if isinstance(error, PendingDownload) else "failed"
                summary[status] += 1
                self.journal.update(entry, status=status, detail=f"{type(error).__name__}: {error}")

        def submit(executor, active: dict, entry: dict, steps) -> None:
            """将 entry 的 steps 交给 executor，登记 future 到 active；无返回值。"""
            future = executor.submit(self._advance, steps)
            future.steps = steps
            active[future] = entry

        try:
            while active_downloads or active_processing or waiting or ready or retries or not exhausted:
                reason = self.dispatch_stop_reason()
                if reason and not exhausted:
                    exhausted = True
                    for task, entry in pending:
                        summary["skipped"] += 1
                        self.journal.update(entry, status="skipped", detail=reason)
                while waiting and (queued_bytes + waiting[0][2] <= capacity or not ready):
                    entry, steps, size = waiting.popleft()
                    ready.append((entry, steps, size))
                    queued_bytes += size
                    self.notify(entry, "waiting_processing", detail="已下载，等待后处理")
                while ready and len(active_processing) < self.config["max_in_flight"]:
                    entry, steps, size = ready.popleft()
                    queued_bytes -= size
                    submit(processing, active_processing, entry, steps)
                while len(active_downloads) + len(waiting) < self.config["download"]["threads"]:
                    if retries:
                        entry, steps = retries.popleft()
                    elif not exhausted:
                        try:
                            task, entry = next(pending)
                        except StopIteration:
                            exhausted = True
                            break
                        steps = self._job_steps(task, entry)
                    else:
                        break
                    submit(downloads, active_downloads, entry, steps)
                futures = set(active_downloads) | set(active_processing)
                if futures:
                    done, _ = wait(futures, timeout=self.interval, return_when=FIRST_COMPLETED)
                    for future in done:
                        entry = active_downloads.pop(future, None)
                        if entry is None:
                            entry = active_processing.pop(future)
                        finish(entry, future)
        except KeyboardInterrupt:
            self.cancel.set()
            downloads.shutdown(wait=True, cancel_futures=True)
            processing.shutdown(wait=True, cancel_futures=True)
            for future, entry in list(active_downloads.items()) + list(active_processing.items()):
                if not future.cancelled():
                    finish(entry, future)
                else:
                    try:
                        next(future.steps)
                    except Exception:
                        pass
                    summary["interrupted"] += 1
                    self.journal.update(entry, status="interrupted", detail="批次中断")
            for entry, steps, _ in list(waiting) + list(ready):
                try:
                    next(steps)
                except Exception:
                    pass
                summary["interrupted"] += 1
                self.journal.update(entry, status="interrupted", detail="批次中断")
            for entry, steps in retries:
                try:
                    next(steps)
                except Exception:
                    pass
                summary["interrupted"] += 1
                self.journal.update(entry, status="interrupted", detail="批次中断")
            for task, entry in pending:
                summary["interrupted"] += 1
                self.journal.update(entry, status="interrupted", detail="中断前未派发")
            raise
        finally:
            self.cancel.set()
            downloads.shutdown(wait=True, cancel_futures=True)
            processing.shutdown(wait=True, cancel_futures=True)
        return summary


def run_url_batch(config: dict) -> dict:
    """按 config 执行当前清单；获取输出/断点锁并导出公共报告，返回完成/复用/失败等状态计数。"""
    with output_lock(config["output_path"]), output_lock(config["work_dir"] / "url"):
        journal = Journal(config)
        print(f"实时日志：{journal.url}", flush=True)
        summary = {"total": 0, "completed": 0, "reused": 0, "failed": 0, "interrupted": 0, "skipped": 0}
        status = "failed"
        batch = None
        try:
            batch = UrlBatch(config, journal)
            tasks, failed = discover(batch.client, requests(config))
            summary["total"], summary["failed"] = len(tasks) + len(failed), len(failed)
            for item in failed:
                entry = journal.add(item)
                journal.update(entry, status="failed", detail=item["detail"])
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
            if batch is not None and batch.authentication is not None:
                with journal.lock:
                    journal.run["authentication"] = batch.authentication.diagnostics()
            print(f"离线报告：{journal.close(status, summary)}", flush=True)
