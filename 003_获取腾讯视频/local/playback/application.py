"""将腾讯 URL 来源接入公共调度、完整解码、进度记录和不覆盖发布；保留每个源版本的断点。"""

from pathlib import Path
import re
import shutil
from threading import Event, Semaphore
from uuid import uuid4

from ..batch import Batch, index_path, load_index
from ..journal import Journal
from ..process import run_process
from ..runtime import TaskCancelled, check_cancel
from ..storage import atomic_json, fingerprint, output_lock, publish, timestamp
from .audio import AudioSelector
from .client import NativeClient
from .errors import AuthenticationError, PlaybackError, SourceChanged
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


def reusable(config: dict, task: dict, source: Resolved, cancel: Event) -> Path | None:
    """检查 task/source 在 config 输出目录登记的已验证 URL 成品；cancel 可取消；返回路径或 None。"""
    location = task_index(config, task)
    for item in reversed(load_index(location)["files"]):
        if (item.get("source_type") != "url" or item.get("video_id") != task["video_id"]
                or item.get("source_identity") != source.identity or item.get("verification", {}).get("exit_code") != 0):
            continue
        path = (location.parent / item["file"]).resolve()
        if not path.is_relative_to(location.parent) or not path.is_file():
            continue
        actual = fingerprint(path, config["copy_buffer_bytes"], force=True, cancel_event=cancel)
        if actual["sha256"] == item.get("sha256"):
            return path
    return None


class UrlBatch(Batch):
    """复用缓存批次的资源槽、媒体探测和完整解码；URL 仅替换来源准备及来源身份检查。"""

    def __init__(self, config: dict, journal: Journal, *, client: NativeClient | None = None):
        """以 config/journal 创建 URL 批次；client 可由测试注入，None 时通过隔离进程只读客户端登录态。"""
        super().__init__(config, journal)
        self.client = NativeClient(config, self.cancel) if client is None else client
        self.auth_stop = Event()
        self.client.transport.auth_stop = self.auth_stop
        self.client.transport.cancel = self.cancel
        self.selector = AudioSelector(config["audio"])
        self.download_slots = Semaphore(config["download"]["threads"])

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
        """处理单集 task 并更新 entry；返回终态报告，来源变化按配置重试，失败和取消保留现场。"""
        config, cancel = self.config, self.cancel
        for attempt in range(config["download"]["refresh_attempts"] + 1):
            work, report = None, {"source_type": "url", "video_id": task["video_id"], "status": "running"}
            try:
                check_cancel(cancel)
                if self.auth_stop.is_set():
                    raise AuthenticationError("登录已失效，当前任务不再发起请求")
                self.notify(entry, "resolving", detail="查询账号授权画质及音质")
                with self.resource(entry, "download", self.download_slots):
                    source = resolve(self.client, task, self.selector)
                    work = config["work_dir"] / "url" / task["series_id"] / task["video_id"] / source.identity
                    work.mkdir(parents=True, exist_ok=True)
                    report.update(source=source.description, source_identity=source.identity, work_dir=str(work))
                    self.journal.update(entry, source=source.description, source_identity=source.identity,
                                        detail=f"{source.description['format']['sname']} / {source.audio.name}；{source.audio.metadata.get('note', '')}")
                    for option in source.description["audio_options"]:
                        if not option["downloadable"]:
                            print(f"[{task['title']}] {option['name']}：{option.get('note', '不可归档')}", flush=True)
                    existing = reusable(config, task, source, cancel)
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
                    index = load_index(index_file)
                    published = False
                    try:
                        publish(candidate, target, output["sha256"], config["copy_buffer_bytes"], cancel_event=cancel,
                                before_publish=lambda: resolve(self.client, task, self.selector, expected=source))
                        published = True
                        index["files"].append({"source_type": "url", "video_id": task["video_id"], "file": target.name,
                            "source_identity": source.identity, "source": source.description,
                            "sha256": output["sha256"], "bytes": output["size_bytes"], "verification": verification,
                            "media": media, "created": timestamp()})
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
            except AuthenticationError:
                self.auth_stop.set()
                report.update(status="failed", error="客户端登录失效，已停止新请求")
                raise
            except SourceChanged as error:
                report.update(status="failed", error=safe_message(error))
                if attempt == config["download"]["refresh_attempts"]:
                    raise
                self.journal.update(entry, detail="来源版本变化，保留旧现场并重新解析")
            except BaseException as error:
                report.update(status="interrupted" if isinstance(error, (TaskCancelled, KeyboardInterrupt)) else "failed",
                              error=safe_message(error))
                raise
            finally:
                if work is not None:
                    atomic_json(work / f"report_{self.journal.run['id']}.json", report)
        raise AssertionError("不可达的 URL 重试状态")


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
            print(f"离线报告：{journal.close(status, summary)}", flush=True)
