"""协调链接任务、有界分阶段并发、完整验证与单一发布，单集失败保留现场并继续其他集。"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from queue import Empty, Queue
import shutil
from threading import BoundedSemaphore, Event
import time
from typing import Any

from .activity import Activity
from .client import NativeClient, check_cancel, normalize_url, safe_message
from .configuration import read_config
from .errors import AuthenticationError, Cancelled, SourceChanged, ToolError
from .hls import download, playlists
from .media import gpu_probe, mux, verify
from .scheduler import run_pipeline
from .series import date_prefix
from .storage import DirectoryLock, Publisher, atomic_json, digest, safe_name
from .tasks import Episode, catalog, describe, requests, resolved, streams


@dataclass
class DownloadedEpisode:
    """下载完成的单集现场；episode/source/identity 标识源，directory/manifests 指向本地现场。

    evidence 包含原始分片字节、时长等证据；started 为单调起始时间，timings 保存各阶段秒数。
    """

    episode: Episode
    source: dict[str, Any]
    identity: str
    directory: Path
    manifests: list[Path]
    evidence: dict[str, Any]
    started: float
    timings: dict[str, float]


class Pipeline:
    """提供独立下载与后处理阶段；共享 config/client/log/publisher，cancel/auth_stop 控制停止。"""

    def __init__(self, config: dict[str, Any], client: NativeClient, log: Activity, publisher: Publisher, cancel: Event, auth_stop: Event):
        """保存各共享服务并初始化封装、验证及发布槽；各参数为本批服务和停止事件；无返回值。"""
        self.config, self.client, self.log, self.publisher, self.cancel, self.auth_stop = config, client, log, publisher, cancel, auth_stop
        self.mux_slots = BoundedSemaphore(config["pipeline"]["mux_slots"])
        self.publish_slots = BoundedSemaphore(config["pipeline"]["publish_slots"])
        self.devices = Queue()
        for device in ("gpu", "cpu"):
            if config["decode_verification"][device]:
                self.devices.put(device)

    def stage(self, episode: Episode, name: str, progress: str = "", message: str = "") -> None:
        """记录 episode 的 name 阶段；progress/message 为脱敏进度和说明，附加清单备注；无返回值。"""
        note = f"（备注：{episode.note}）" if episode.note else ""
        self.log.update(episode.key, f"{episode.series} · 第{episode.index}集", name, progress, message + note)

    def failure(self, episode: Episode, error: Exception) -> str:
        """结算 episode 的 error；认证错误停止新下载，取消保留现场；返回已记录的终态字符串。"""
        if isinstance(error, AuthenticationError):
            self.auth_stop.set()
            status = "认证失效"
        elif isinstance(error, Cancelled):
            status = "取消"
        else:
            status = "失败"
        self.stage(episode, status, message=safe_message(error))
        return status

    def acquire(self, semaphore: BoundedSemaphore) -> None:
        """可取消地获取 semaphore；参数为阶段信号量；成功无返回值。"""
        while not semaphore.acquire(timeout=self.config["runtime"]["cancel_poll_interval_seconds"]):
            check_cancel(self.cancel)
        if self.cancel.is_set():
            semaphore.release()
            check_cancel(self.cancel)

    def device(self) -> str:
        """等待空闲解码后端；无参数；返回 cpu/gpu，取消时停止等待。"""
        while True:
            check_cancel(self.cancel)
            try:
                return self.devices.get(timeout=self.config["runtime"]["cancel_poll_interval_seconds"])
            except Empty:
                continue

    def download_episode(self, episode: Episode) -> DownloadedEpisode | str:
        """下载 episode 并按配置重试源变化；返回现场载荷或复用/认证失效/失败终态，其他异常交调度器结算。"""
        for attempt in range(self.config["download"]["refresh_attempts"] + 1):
            try:
                return self.transfer(episode)
            except AuthenticationError:
                self.auth_stop.set()
                raise
            except SourceChanged as error:
                self.stage(episode, "源版本变化", message=safe_message(error))
                if attempt == self.config["download"]["refresh_attempts"]:
                    return self.failure(episode, ToolError("源持续变化，已保留全部版本现场"))
        raise AssertionError("不可达的源刷新状态")

    def transfer(self, episode: Episode) -> DownloadedEpisode | str:
        """解析并顺序下载单集 episode；返回完整现场或复用/认证失效终态，不占用后处理资源。"""
        check_cancel(self.cancel)
        started = time.monotonic()
        if self.auth_stop.is_set():
            self.stage(episode, "未开始", message="登录失效，已停止派发")
            return "认证失效"
        self.stage(episode, "解析画质")
        stream = resolved(self.client, episode)
        source = describe(stream)
        media = playlists(self.client.transport, stream["disp"]["info"])
        expected_duration = float(stream["ftime"])
        if any(abs(item.duration - expected_duration) > self.config["verification"]["duration_tolerance_seconds"] for item in media):
            raise ToolError("源清单时长与正片元数据不符，拒绝下载预览片段")
        identity = digest({"clip_id": episode.clip_id, "video_id": episode.video_id, "source": source,
                           "playlists": [item.identity for item in media]})
        self.stage(episode, "检查复用")
        record = self.publisher.reusable(episode.key, identity)
        if record:
            self.stage(episode, "复用", message=record["path"])
            return "复用"
        directory = self.config["work_dir"] / episode.clip_id / episode.video_id / identity
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / "source.json", {"episode": asdict(episode), "source": source, "identity": identity}, self.config["logging"])
        self.stage(episode, "下载", message=f"{source['standardName']} · {source['videoFormat']} · {source['videoWidth']}×{source['videoHeight']}")
        before = time.monotonic()

        def refresh():
            """使用相同具体源续签；无参数；返回新内存清单，源变化由下层明确拒绝。"""
            renewed = resolved(self.client, episode, stream)
            return playlists(self.client.transport, renewed["disp"]["info"])

        manifests, evidence = download(self.client.transport, media, directory, self.config, refresh,
                                       lambda done, total: self.stage(episode, "下载", f"{done}/{total}"), self.cancel)
        return DownloadedEpisode(episode, source, identity, directory, manifests, evidence, started,
                                 {"download_seconds": round(time.monotonic() - before, 3)})

    def process_episode(self, task: DownloadedEpisode) -> str:
        """对 task 的完整现场封装、验证、发布并按配置清理；返回成功，异常交调度器记录且保留现场。"""
        episode, directory, source, timings = task.episode, task.directory, task.source, task.timings
        self.acquire(self.mux_slots)
        try:
            self.stage(episode, "封装")
            before = time.monotonic()
            candidate = directory / "candidate.mp4"
            mux(self.config, task.manifests, candidate, self.cancel)
            timings["mux_seconds"] = round(time.monotonic() - before, 3)
        finally:
            self.mux_slots.release()
        self.stage(episode, "等待验证")
        device = self.device()
        try:
            self.stage(episode, f"完整解码({device})")
            validation = verify(self.config, candidate, device, task.evidence["durations"], source, self.cancel)
        finally:
            self.devices.put(device)
        self.acquire(self.publish_slots)
        try:
            self.stage(episode, "发布")
            naming = self.config["naming"]
            context = {"clip_id": episode.clip_id, "video_id": episode.video_id, "index": episode.index,
                       "title": safe_name(episode.series, naming["max_title_length"])}
            series_stem = date_prefix(naming, episode.release_date) + naming["series_template"].format_map(context)
            if len(series_stem) > naming["max_filename_length"]:
                raise ToolError("剧集目录名超过 naming.max_filename_length，请调整标题长度或模板")
            series = safe_name(series_stem, naming["max_filename_length"])
            context["title"] = safe_name(episode.title, naming["max_title_length"])
            context["quality"] = safe_name(f"{source['standardName']}_{source['videoFormat']}_{source['videoWidth']}x{source['videoHeight']}" + ("_HDR" if source.get("isHdr") else "_SDR"), naming["max_title_length"])
            stem = naming["episode_template"].format_map(context)
            if len(stem) > naming["max_filename_length"]:
                raise ToolError("成品文件名超过 naming.max_filename_length，请调整标题长度或模板")
            filename = safe_name(stem, naming["max_filename_length"]) + ".mp4"
            timings["total_seconds"] = round(time.monotonic() - task.started, 3)
            record = self.publisher.publish(candidate, Path(series) / filename, episode.key, task.identity,
                                             {"source": source, "hls": task.evidence, "verification": validation, "timings": timings})
            try:
                atomic_json(directory / "report.json", record, self.config["logging"])
            except OSError:
                self.stage(episode, "发布完成", message="成品与索引已提交，工作目录副本报告写入失败")
        finally:
            self.publish_slots.release()
        self.stage(episode, "成功", message=record["path"])
        if not self.config["download"]["keep_intermediates"]:
            try:
                for track in directory.glob("track_*"):
                    if track.is_dir():
                        shutil.rmtree(track)
                candidate.unlink(missing_ok=True)
                (directory / "segments.json").unlink(missing_ok=True)
            except OSError:
                self.stage(episode, "成功", message=record["path"] + "；部分中间文件清理失败")
        return "成功"


def run(args) -> int:
    """执行入口解析后的 args；加载配置并运行模式；返回 0 成功、1 失败或 130 中断。"""
    config = read_config(args.config)
    cancel, auth_stop = Event(), Event()
    client = NativeClient(config, cancel)
    if args.login_only:
        _, clip_id, video_id = normalize_url(config["auth"]["probe_url"])
        account = client.login_status(clip_id, video_id)
        print(json.dumps({"authenticated": True, "isVip": account.get("isVip")}, ensure_ascii=False))
        return 0
    values = args.url or [input("请输入芒果链接或 YAML 下载清单路径：").strip().strip('"')]
    tasks = requests(values, config, args.all_parts)
    if args.list_formats:
        failed, seen = False, set()
        for task in tasks:
            try:
                for episode in catalog(client, task):
                    if episode.key in seen:
                        continue
                    seen.add(episode.key)
                    if not episode.released:
                        print(f"[待上线] {episode.series} 第{episode.index}集")
                        continue
                    try:
                        values = streams(client.source(episode.clip_id, episode.video_id))
                        print(json.dumps({"episode": asdict(episode), "formats": [{"rank": index, **describe(value)} for index, value in enumerate(values, 1)]}, ensure_ascii=False), flush=True)
                    except AuthenticationError:
                        raise
                    except ToolError as error:
                        failed = True
                        print(f"[失败] {episode.key}：{safe_message(error)}", flush=True)
            except AuthenticationError:
                raise
            except ToolError as error:
                failed = True
                print(f"[选集失败] {safe_message(error)}", flush=True)
        return int(failed)
    config["output_path"].mkdir(parents=True, exist_ok=True)
    config["work_dir"].mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(DirectoryLock(config["output_path"] / config["logging"]["lock_file"]))
        stack.enter_context(DirectoryLock(config["work_dir"] / config["logging"]["work_lock_file"]))
        log = Activity(config)
        counts = {}
        interrupted, failed = False, False
        try:
            print(f"实时记录：{log.start()}", flush=True)
            publisher = Publisher(config, cancel)
            if config["decode_verification"]["gpu"]:
                gpu_probe(config, config["work_dir"], cancel)
            pipeline = Pipeline(config, client, log, publisher, cancel, auth_stop)
            seen = {}

            def incoming():
                """按清单顺序增量展开；无参数；逐集返回，目录失败记录后继续后续任务。"""
                nonlocal failed
                for index, task in enumerate(tasks):
                    if auth_stop.is_set() or cancel.is_set():
                        return
                    try:
                        for episode in catalog(client, task):
                            if episode.key in seen:
                                if seen[episode.key] != episode.quality:
                                    raise ToolError("重复视频画质配置冲突")
                                continue
                            seen[episode.key] = episode.quality
                            if not episode.released:
                                log.update(episode.key, episode.title, "待上线")
                                counts["待上线"] = counts.get("待上线", 0) + 1
                                continue
                            yield episode
                    except AuthenticationError as error:
                        auth_stop.set()
                        failed = True
                        log.update(f"catalog/{index}", task.note or task.url, "认证失效", message=safe_message(error))
                        return
                    except ToolError as error:
                        failed = True
                        log.update(f"catalog/{index}", task.note or task.url, "选集失败", message=safe_message(error))

            def complete(episode: Episode, status: str | None, error: Exception | None) -> None:
                """在主线程结算 episode 的 status/error 并更新汇总；无返回值。"""
                nonlocal failed
                if error is not None:
                    status = pipeline.failure(episode, error)
                counts[status] = counts.get(status, 0) + 1
                failed |= status in ("失败", "认证失效")

            run_pipeline(incoming(), config, pipeline.download_episode, pipeline.process_episode, complete, cancel, auth_stop,
                         downloaded_size=lambda task: task.evidence["bytes"], waiting=pipeline.stage)
            if auth_stop.is_set():
                failed = True
        except (KeyboardInterrupt, Cancelled):
            cancel.set()
            interrupted = True
            print("任务已取消，线程与子进程已回收，断点已保留。", flush=True)
        except Exception as error:
            failed = True
            log.update("batch", "批次", "失败", message=safe_message(error))
        finally:
            report = log.finish("已取消" if interrupted else "部分失败" if failed else "已完成")
            print(f"结果：{json.dumps(counts, ensure_ascii=False)}\n离线报告：{report}", flush=True)
        return 130 if interrupted else int(failed)
