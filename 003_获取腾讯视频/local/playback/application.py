"""将腾讯 URL 来源接入公共调度、完整解码、进度记录和不覆盖发布；保留每个源版本的断点。"""

from pathlib import Path
import re
import shutil
from threading import Event
from uuid import uuid4

from ..batch import Batch, PendingDownload, check_record, index_path, load_index
from ..journal import Journal
from ..process import run_process
from ..runtime import TaskCancelled, check_cancel
from ..storage import atomic_json, fingerprint, output_lock, publish, timestamp
from .audio import AudioSelector
from .client import NativeClient
from .errors import AuthenticationError, AuthenticationStopped, PlaybackError, SourceChanged
from .hls import download_steps
from .source import Resolved, resolve
from .tasks import Discovery, requests
from .scheduler import UrlScheduler
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


class UrlBatch(UrlScheduler, Batch):
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
        """分阶段处理 task 并更新 entry；让出下载、续签、待处理字节数或重试标记，结束时返回报告。"""
        config, cancel = self.config, self.cancel
        for attempt in range(config["download"]["refresh_attempts"] + 1):
            work, report = None, {"source_type": "url", "video_id": task["video_id"], "status": "running"}
            try:
                check_cancel(cancel)
                if self.auth_stop.is_set():
                    raise AuthenticationStopped("认证恢复失败，当前任务停止请求并保留现场")
                self.notify(entry, "resolving", detail="查询账号授权画质及音质")
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
                yield "download", 0
                segments = download_steps(self.client.transport, source.manifests, work, config,
                    lambda done, total: self.journal.update(entry, progress=done * 100 / total), cancel,
                    telemetry=lambda **values: self.journal.update(entry, **values))
                refreshed = None
                try:
                    while True:
                        check_cancel(cancel)
                        self.notify(entry, "downloading", detail=f"下载 {source.description['format']['sname']} / {source.audio.name}", work_dir=str(work))
                        try:
                            segments.send(refreshed)
                        except StopIteration as done:
                            manifests, evidence = done.value
                            break
                        yield "resolve", 0
                        check_cancel(cancel)
                        self.notify(entry, "resolving", detail="媒体地址过期，续签同源清单")
                        refreshed = resolve(self.client, task, self.selector, expected=source).manifests
                        yield "download", 0
                finally:
                    segments.close()
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
                report.update(status="interrupted" if isinstance(error, (TaskCancelled, KeyboardInterrupt, GeneratorExit)) else "failed",
                              error=safe_message(error))
                raise
            finally:
                if self.authentication is not None:
                    report["authentication"] = self.authentication.diagnostics()
                    self.journal.update(entry, authentication=report["authentication"])
                if work is not None:
                    atomic_json(work / f"report_{self.journal.run['id']}.json", report)
        raise AssertionError("不可达的 URL 重试状态")


def run_url_batch(config: dict) -> dict:
    """按 config 执行当前清单；以 logging.lock_file 获取本平台输出/断点锁，返回批次状态计数及保存报告。"""
    with output_lock(config["output_path"], config["logging"]["lock_file"]), output_lock(config["work_dir"] / "url", config["logging"]["lock_file"]):
        journal = Journal(config)
        print(f"实时日志：{journal.url}", flush=True)
        summary = {"total": 0, "completed": 0, "reused": 0, "failed": 0, "interrupted": 0, "skipped": 0}
        status = "failed"
        batch = None
        try:
            batch = UrlBatch(config, journal)
            summary.update(batch.run([], discovery=Discovery(batch.client, requests(config), batch.cancel)))
            status = "warning" if summary["failed"] or summary["skipped"] or summary["interrupted"] else "completed"
            return summary
        except KeyboardInterrupt:
            status = "interrupted"
            summary.update(getattr(batch, "summary", {}))
            raise
        except Exception:
            summary.update(getattr(batch, "summary", {}))
            raise
        finally:
            if batch is not None and batch.authentication is not None:
                with journal.lock:
                    journal.run["authentication"] = batch.authentication.diagnostics()
            print(f"离线报告：{journal.close(status, summary)}", flush=True)
