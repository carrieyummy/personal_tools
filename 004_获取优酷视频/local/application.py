"""优酷 URL 批次流水线：并发下载、原码流还原、完整验证和不覆盖发布。"""

from pathlib import Path
import shutil
from threading import Semaphore
from uuid import uuid4

from .batch import Batch, check_record, index_path, load_index
from .configuration import safe_name
from .download import download_steps
from .errors import safe_message
from .journal import Journal
from .process import run_process
from .runtime import check_cancel, TaskCancelled
from .scheduler import Scheduler, PendingDownload
from .storage import atomic_json, fingerprint, output_lock, publish, read_json, timestamp
from .tasks import discover, requests
from .youku.client import Client, AuthenticationError, PlaybackError, SourceChanged
from .youku.source import resolve
from .youku.transport import restore_files, normalize_continuity


def verification_valid(proof: dict, duration: float, tolerance: float) -> bool:
    """核验完整解码 proof 是否具有真实视频/音频帧及目标 duration；tolerance 为秒数容差，返回布尔值。"""
    audio=proof.get("audio_frames",{})
    times=proof.get("audio_duration_seconds",{})
    return (proof.get("exit_code")==0 and type(proof.get("frames")) is int and proof["frames"]>0
            and proof.get("device") in ("cpu","gpu") and isinstance(audio,dict) and bool(audio)
            and all(type(value) is int and value>0 for value in audio.values())
            and isinstance(times,dict) and set(map(str,times))==set(map(str,audio))
            and all(type(value) in (int,float) and abs(value-duration)<=tolerance for value in times.values())
            and type(proof.get("duration_seconds")) in (int,float)
            and abs(proof["duration_seconds"]-duration)<=tolerance)


def cleanup_media(work: Path) -> str | None:
    """在已提交成品后清理 work 内媒体；保留日志、帧哈希和验证证明，返回清理错误说明或 None。"""
    boundary=work.resolve()
    try:
        segments=(work / "segments").resolve()
        processing=(work / "processing").resolve()
        if any(path==boundary or not path.is_relative_to(boundary) for path in (segments,processing)):
            return "拒绝清理越界现场；成品已提交"
        if segments.exists():
            shutil.rmtree(segments)
        for path in processing.rglob("*"):
            if path.suffix in (".ts",".m3u8",".mp4",".ffconcat"):
                if not path.resolve().is_relative_to(boundary):
                    return "拒绝清理越界媒体；成品已提交"
                path.unlink()
        (work / "segments.json").unlink(missing_ok=True)
        return None
    except OSError:
        return "中间媒体清理失败；成品已提交，现场保留"


class UrlBatch(Batch, Scheduler):
    """每集隔离现场；下载独立限流，解析、续签和后处理共享处理池及各阶段资源。"""

    def __init__(self, config: dict, journal: Journal, client=None):
        """以 config/journal 初始化批次；client 可由隔离测试注入，省略时读取本机客户端。"""
        super().__init__(config, journal)
        self.client = Client(config, self.cancel) if client is None else client
        self.restore_slot = Semaphore(1)

    def dispatch_stop_reason(self) -> str | None:
        """返回共享认证失效后的停止派发原因；认证可用返回 None。"""
        return "认证恢复失败，未继续派发" if self.client.stopped.is_set() else None

    def verified_candidate(self, source, work: Path) -> tuple[Path, dict] | None:
        """复核 work 中 source 对应的候选证明和完整摘要；返回候选与证明或 None，不信任单独成功标记。"""
        location = work / "candidate.json"
        if not location.exists():
            return None
        proof = read_json(location)
        verification = proof.get("verification", {})
        if (proof.get("source_identity") != source.identity or not verification_valid(
                verification,source.duration,self.config["duration_tolerance_seconds"])):
            return None
        candidate = (work / proof["candidate"]).resolve()
        if not candidate.is_relative_to((work / "processing").resolve()) or candidate.suffix != ".mp4" or not candidate.is_file():
            return None
        actual = fingerprint(candidate,self.config["copy_buffer_bytes"],force=True,cancel_event=self.cancel)
        if actual != proof.get("fingerprint"):
            return None
        self.check_media(proof["media"],source)
        return candidate,proof

    def reuse(self, task: dict, source) -> Path | None:
        """核验 task/source 已登记成品的来源、证明和文件指纹；返回可复用路径或 None。"""
        location = index_path(self.config, task)
        with self.index_lock:
            index = load_index(location)
        for record in reversed(index["files"]):
            if (record.get("platform") != "youku" or record.get("source_identity") != source.identity
                    or record.get("video_id") != task["video_id"] or not verification_valid(
                        record.get("verification",{}),source.duration,self.config["duration_tolerance_seconds"])):
                continue
            path = (location.parent / record["file"]).resolve()
            if path.is_relative_to(location.parent) and path.is_file() and check_record(
                    self.config, location, path, record, self.cancel, self.index_lock):
                return path
        return None

    def restore(self, source, paths: list[Path], work: Path, processing: Path, entry: dict) -> list[Path]:
        """校验 paths 后还原至 processing；按 source 不连续区间生成清单，返回清单路径列表。"""
        self.notify(entry,"restoring",detail="按完整 PES 还原原始音视频")
        target = processing / "restored.ts"
        with self.resource(entry,"restoration",self.restore_slot):
            records = read_json(work / "segments.json")["segments"]
            stats = restore_files(paths,source.segments,records,source.key,target,
                                  self.config["copy_buffer_bytes"],self.config["playback"]["media"]["max_pes_bytes"],self.cancel)
            normalize_continuity(target,self.cancel)
        atomic_json(processing / "restoration.json",stats,self.config["storage"])
        import math
        header = ["#EXTM3U","#EXT-X-VERSION:4",f"#EXT-X-TARGETDURATION:{math.ceil(max(s['duration'] for s in source.segments))}","#EXT-X-MEDIA-SEQUENCE:0"]
        groups, lines = [], list(header)
        offset, previous = 0, None
        for index, segment in enumerate(source.segments):
            if previous is not None and previous != segment["discontinuity"]:
                groups.append(lines+["#EXT-X-ENDLIST"])
                lines = list(header)
            size = records[str(index)]["bytes"]
            lines += [f"#EXTINF:{segment['duration']:.9f},",f"#EXT-X-BYTERANGE:{size}@{offset}",target.name]
            previous = segment["discontinuity"]
            offset += size
        groups.append(lines+["#EXT-X-ENDLIST"])
        manifests=[]
        for index,lines in enumerate(groups):
            manifest=processing / f"group_{index:03d}.m3u8"
            manifest.write_text("\n".join(lines)+"\n",encoding="utf-8")
            manifests.append(manifest)
        return manifests

    def mux_candidate(self, manifests: list[Path], processing: Path, entry: dict) -> Path:
        """逐连续区间封装 manifests 再无损拼接到 processing；entry 接收阶段，返回候选 MP4。"""
        self.notify(entry,"muxing",detail="无重编码封装 MP4")
        candidate = processing / "candidate.mp4"
        with self.resource(entry,"mux",self.mux):
            base=[str(self.config["ffmpeg_path"]),"-hide_banner","-nostdin","-nostats","-loglevel","warning","-n"]
            groups=[]
            for index,manifest in enumerate(manifests):
                group=processing / f"group_{index:03d}.mp4"
                command=base+["-protocol_whitelist","file","-allowed_extensions","ALL","-i",str(manifest),
                              "-map","0:v:0","-map","0:a:0","-c","copy","-tag:v","hvc1","-avoid_negative_ts","make_zero",str(group)]
                run_process(command,processing / f"mux_{index:03d}.log",self.config,cancel_event=self.cancel)
                groups.append("file '"+group.name+"'")
            playlist=processing / "groups.ffconcat"
            playlist.write_text("ffconcat version 1.0\n"+"\n".join(groups)+"\n",encoding="utf-8")
            command=base+["-protocol_whitelist","file","-f","concat","-safe","1","-i",str(playlist),
                          "-map","0:v:0","-map","0:a:0","-c","copy","-tag:v","hvc1","-movflags","+faststart",str(candidate)]
            run_process(command,processing / "mux.log",self.config,cancel_event=self.cancel)
        return candidate

    def check_media(self, media: dict, source) -> None:
        """将实际 media 对照 source 的尺寸、编码、位深和音轨；成功无返回值，不接受降规格。"""
        expected, adapter = source.description, source.description["adapter"]
        if (media["width"] != expected["width"] or media["height"] != expected["height"]
                or media["video_codec"] != adapter["video_codec"] or media["pixel_format"] != adapter["pixel_format"]
                or media["audio_codecs"] != [adapter["audio_codec"]] or not media["fps"]
                or media["duration_seconds"] is None
                or abs(media["duration_seconds"]-source.duration)>self.config["duration_tolerance_seconds"]):
            raise PlaybackError("成品实际规格或完整时长与所选原始媒体不符")

    def _job_steps(self, task: dict, entry: dict):
        """推进 task 的解析、下载及后处理；entry 接收状态，阶段标记切换资源池，结束返回终态报告。"""
        config = self.config
        for attempt in range(config["download"]["refresh_attempts"] + 1):
            work = None
            report = {"platform":"youku","source_type":"url","video_id":task["video_id"],"status":"running"}
            try:
                check_cancel(self.cancel)
                self.notify(entry,"resolving",detail="独立解析本集正片画质、音轨与授权")
                source = resolve(self.client,task)
                work = config["work_dir"] / "url" / task["series_id"] / safe_name(task["video_id"]) / source.identity
                work.mkdir(parents=True,exist_ok=True)
                report.update(source=source.description,source_identity=source.identity,work_dir=str(work))
                self.journal.update(entry,source=source.description,source_identity=source.identity,work_dir=str(work))
                existing = self.reuse(task,source)
                if existing:
                    return {**report,"status":"reused","output":str(existing)}
                atomic_json(work / "source.json",{"identity":source.identity,"task":task,"source":source.description},config["storage"])
                resumed = self.verified_candidate(source,work)
                if resumed:
                    yield "ready", resumed[1]["fingerprint"]["size_bytes"]
                else:
                    yield "download", 0
                    segments = download_steps(source,work,config,self.cancel,
                        lambda **values:self.journal.update(entry,**values))
                    refreshed = None
                    try:
                        while True:
                            check_cancel(self.cancel)
                            self.notify(entry,"downloading",detail="下载并校验完整正片分段")
                            try:
                                segments.send(refreshed)
                            except StopIteration as done:
                                paths, evidence = done.value
                                break
                            yield "resolve", 0
                            check_cancel(self.cancel)
                            self.notify(entry,"resolving",detail="媒体地址过期，重新解析同源地址")
                            refreshed = resolve(self.client,task,expected=source)
                            yield "download", 0
                    finally:
                        segments.close()
                    report["download"] = evidence
                    yield "ready", evidence["bytes"]
                check_cancel(self.cancel)
                processing = work / "processing" / uuid4().hex
                processing.mkdir(parents=True)
                if resumed:
                    candidate, proof = resumed
                    media = self.media(candidate,processing,"resumed_media")
                    self.check_media(media,source)
                    if media != proof["media"]:
                        raise PlaybackError("待续发候选的实际媒体规格已变化")
                    verification = proof["verification"]
                else:
                    manifest = self.restore(source,paths,work,processing,entry)
                    original_media = self.media(processing / "restored.ts",processing,"original_media")
                    candidate = self.mux_candidate(manifest,processing,entry)
                    media = self.media(candidate,processing,"media")
                    self.check_media(media,source)
                    if (original_media["fps"] is None or abs(media["fps"]-original_media["fps"]) > config["verification"]["fps_tolerance"]
                            or any(media[key] != original_media[key] for key in
                                   ("video_codec","width","height","pixel_format","audio_codecs"))
                            or [[track[k] for k in ("codec","sample_rate","channels")] for track in media["audio_streams"]]
                            != [[track[k] for k in ("codec","sample_rate","channels")] for track in original_media["audio_streams"]]):
                        raise PlaybackError("无损封装前后的实际音视频规格不一致")
                    verification = self.verify(candidate,processing,source.duration,entry)
                self.notify(entry,"checking_source",detail="复核正片来源并保存候选证明")
                with self.resource(entry,"source_check",self.check):
                    resolve(self.client,task,expected=source)
                    output = fingerprint(candidate,config["copy_buffer_bytes"],force=True,cancel_event=self.cancel)
                proof = {"source_identity":source.identity,"candidate":str(candidate.relative_to(work)),
                         "fingerprint":output,"media":media,"verification":verification}
                atomic_json(work / "candidate.json",proof,config["storage"])
                report.update(candidate=proof)
                self.notify(entry,"publishing",detail="校验复制并登记成品")
                with self.resource(entry,"publish",self.publish_slot):
                    location = index_path(config,task)
                    location.parent.mkdir(parents=True,exist_ok=True)
                    name = config["naming"]["episode_template"].format(index=task["index"],
                        title=safe_name(task["title"])[:config["naming"]["max_title_length"]],
                        quality=safe_name(source.description["quality"]),audio=safe_name(source.description["audio"]),
                        video_id=safe_name(task["video_id"]))
                    if safe_name(name) != name:
                        raise PlaybackError("成品名称含非法字符")
                    target = location.parent / f"{name}.mp4"
                    if target.exists():
                        target = location.parent / f"{name}_{source.identity[:12]}_{uuid4().hex[:8]}.mp4"
                    published = False
                    try:
                        result = publish(candidate,target,output["sha256"],config["copy_buffer_bytes"],cancel_event=self.cancel)
                        published = True
                        record = {"platform":"youku","source_type":"url","video_id":task["video_id"],"file":target.name,
                                  "source_identity":source.identity,"source":source.description,"verification":verification,
                                  "media":media,"sha256":result["sha256"],"bytes":result["size_bytes"],
                                  "mtime_ns":result["mtime_ns"],"created":timestamp()}
                        with self.index_lock:
                            index = load_index(location)
                            index["files"].append(record)
                            atomic_json(location,index,config["storage"])
                    except BaseException:
                        if published:
                            target.unlink(missing_ok=True)
                        raise
                report.update(status="completed",output=str(target),verification=verification,media=media,sha256=output["sha256"])
                if not config["keep_intermediates"]:
                    error=cleanup_media(work)
                    if error:
                        report["cleanup_error"]=error
                return report
            except SourceChanged:
                if attempt == config["download"]["refresh_attempts"]:
                    raise
                self.notify(entry,"resolving",detail="来源变化，保留旧现场并重新解析")
                yield "retry", 0
            except BaseException as error:
                report.update(status="interrupted" if isinstance(error,(TaskCancelled,KeyboardInterrupt,GeneratorExit)) else "failed",error=safe_message(error))
                raise
            finally:
                if work is not None:
                    report["authentication"] = self.client.diagnostics()
                    atomic_json(work / f"report_{self.journal.run['id']}.json",report,config["storage"])


def run_url_batch(config: dict) -> dict:
    """按 config 执行清单并以 logging.lock_file 持有本平台输出/工作锁；返回计数，异常时仍保存页面和日志。"""
    with output_lock(config["output_path"], config["logging"]["lock_file"]), output_lock(config["work_dir"] / "url", config["logging"]["lock_file"]):
        journal = Journal(config)
        print(f"实时日志：{journal.url}",flush=True)
        status = "failed"
        summary = {"total":0,"completed":0,"reused":0,"failed":0,"interrupted":0,"skipped":0}
        batch = None
        try:
            batch = UrlBatch(config,journal)
            tasks, failed = discover(batch.client,requests(config))
            summary.update(total=len(tasks)+len(failed),failed=len(failed))
            for failure in failed:
                journal.update(journal.add(failure),status="failed",detail=failure["detail"])
            for key, count in batch.run(tasks).items():
                summary[key] += count
            status = "warning" if summary["failed"] else "completed"
            return summary
        except KeyboardInterrupt:
            status = "interrupted"
            if batch:
                summary.update(batch.summary)
            raise
        except Exception as error:
            journal.run["error"] = safe_message(error)
            raise
        finally:
            print(f"离线报告：{journal.close(status,summary)}",flush=True)
