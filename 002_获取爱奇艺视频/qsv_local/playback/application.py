"""按 YAML 清单顺序获取链接视频，原生会话仅初始化一次；单项失败留现场并继续其余任务。"""

from datetime import datetime
from functools import partial
from pathlib import Path
from threading import Event

import yaml
import webbrowser

from ..configuration import resolve_path
from ..activity_log import Journal
from ..storage import atomic_json, output_lock
from ..runtime import TaskCancelled, check_cancel
from .configuration import read_profile
from .metadata import linked_videos, require_same_metadata
from .coordination import StageControls
from .cancellation import CancellationRegistry
from .download import download_steps, manifest_identity
from .listing import expand_episodes, task_options
from .media import media_config
from .manifests import selected_media_plan
from .outputs import reusable_output
from .source import select_av_source
from .scheduler import schedule
from .records import consolidate_output
from .retry import clear_episode_work


def plan_batches(profile: dict, indexes: list[int] | None, episode_orders: list[int] | None = None, cancel=None, *, options=None, registry=None):
    """逐链接交接目录；options 为可选清单快照，registry 管理局部取消；返回任务、错误及进度。"""
    options = task_options(profile) if options is None else options
    chosen = list(range(len(options))) if indexes is None else indexes
    if not chosen or len(set(chosen)) != len(chosen) or any(type(value) is not int or value < 0 or value >= len(options) for value in chosen):
        raise ValueError("链接下标为空、重复或超出当前清单")
    if episode_orders is not None and (not episode_orders or len(set(episode_orders)) != len(episode_orders)
                                      or any(type(value) is not int or value <= 0 for value in episode_orders)):
        raise ValueError("集序过滤必须为不重复的正整数列表")
    identities = set()
    for position, index in enumerate(chosen, 1):
        check_cancel(cancel)
        jobs, errors = [], []
        option = options[index]
        try:
            stopped = registry.begin(index) if registry else cancel
            check_cancel(stopped)
            link = linked_videos(profile, [index], urls=[item["url"] for item in options])[0] if registry else linked_videos(profile, [index])[0]
            stopped = registry.resolved(index, link) if registry else cancel
            check_cancel(stopped)
            episodes = expand_episodes(profile, link, option["all_parts"], cancel=stopped)
            check_cancel(stopped)
            if episode_orders is not None:
                if not set(episode_orders) <= {video["episode"] for video in episodes}:
                    raise ValueError("当前任务按 all-parts 展开后不含所指定的集序")
                episodes = [video for video in episodes if video["episode"] in episode_orders]
            for video in episodes:
                key = (video["tvid"], option["quality"])
                if key not in identities:
                    identities.add(key)
                    jobs.append({"video": video, "quality": option["quality"]})
            if registry:
                registry.finish(index, "ready")
        except TaskCancelled:
            if cancel is not None and cancel.is_set():
                raise
            if registry:
                registry.finish(index, "cancelled")
        except Exception as error:
            if registry and stopped.is_set():
                registry.finish(index, "cancelled")
            else:
                if registry:
                    registry.finish(index, "failed")
                errors.append({"index": index, "stage": "resolving_link", "error": str(error)})
        yield {"jobs":jobs, "errors":errors, "processed":position, "total":len(chosen)}


def build_plan(profile: dict, indexes: list[int] | None, episode_orders: list[int] | None = None) -> tuple[list[dict], list[dict]]:
    """同步收集 profile/indexes 的公开计划；episode_orders 可筛选集序，返回去重任务及错误；不获取媒体。"""
    jobs, errors = [], []
    for batch in plan_batches(profile, indexes, episode_orders):
        jobs.extend(batch["jobs"])
        errors.extend(batch["errors"])
    return jobs, errors


def job_steps(profile: dict, config: dict, work_root: Path, job: dict, controls: StageControls, progress=None):
    """解析 job 并获取资源；profile/config 控制媒体，work_root 保存断点，controls 限制并发，progress 接收事件。

    job 中的 _retry_from 表示清理该集此前的来源工作目录（可以为 None）并重新下载；让出下载/处理阶段并返回结果。
    """
    video = job["video"]
    retrying = "_retry_from" in job
    previous = job.get("_retry_from")
    if retrying:
        output_root = resolve_path(config["output_path"], profile["main_config"].parent)
        if progress:
            progress({"stage": "resetting_download", "detail": "清理本集旧断点"})
        if previous:
            with controls.identity(previous):
                clear_episode_work(work_root, output_root, video["tvid"], previous)
            job["_work_key"] = None
    if progress:
        progress({"stage": "waiting_source", "detail": "等待原生播放资源接口"})
    require_same_metadata(profile, video)
    # 原生请求拦截和 SDK 授权验证均未证明可并发，统一保护。
    with controls.slot("source"), controls.slot("sdk"):
        if progress:
            progress({"stage": "resolving_source", "detail": "解析最高视频与最高音轨"})
        result = select_av_source(profile, video, job["quality"], config["audio"]["quality"])
    source, selection = result.selected, result.report["selection"]
    if progress:
        codec = {1: "HEVC", 2: "AVC", 5: "AV1"}[source["code"]]
        quality = "帧绮映画" if source.get("zqyh") == 1 else "增强画质" if source["dr"] > 0 else "标准画质"
        progress({"stage": "authorizing", "detail": "视频与独立音轨均已精确选档", "quality_label": quality,
                  "media_label": f"{source['scrsz']} · {quality} · {codec} · {source['_audio_selection']['codec'].upper()} 音轨排名 {config['audio']['quality']}"})
    plan = selected_media_plan(result.manifest, source, profile)
    identity = manifest_identity(profile, video, source, plan["segments"])
    key = video["tvid"] + "_" + identity["sha256"]
    job["_work_key"] = key
    with controls.identity(key):
        completed = None
        if retrying:
            if previous != key:
                clear_episode_work(work_root, output_root, video["tvid"], key)
        else:
            if progress:
                progress({"stage": "checking_reuse", "detail": "核对既有成品来源、文件名和大小"})
            completed = reusable_output(profile, config, video, source, identity, selection)
        if completed is None:
            work = work_root / key
            completed = yield from download_steps(profile, video, source, result.manifest, work, result.headers, selection, controls, progress)
    return completed


def run_downloads(path: Path, indexes: list[int] | None = None, plan_only: bool = False,
                  episode_orders: list[int] | None = None) -> tuple[dict, Path]:
    """按 path 处理 indexes 链接，episode_orders 可过滤集序；plan_only 只查询公开目录。

    正式下载先合并旧逐集索引，队列结束后保留网页直到用户停止服务；返回批次报告及路径。
    """
    profile = read_profile(path)
    main = yaml.safe_load(profile["main_config"].read_text(encoding="utf-8-sig"))
    work_root = resolve_path(main["work_dir"], profile["main_config"].parent) / "playback_downloads"
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    output = work_root / ("plan_" if plan_only else "batch_")
    output = output.with_name(output.name + stamp + ".json")
    report = {"mode": "plan" if plan_only else "native_program_download", "total_episodes": 0, "completed": 0,
              "reused": 0, "failed": 0, "cancelled": 0, "link_errors": [], "jobs": [], "results": [], "status": "planned" if plan_only else "running",
              "visible_player_switched": False, "download_tasks_created": False}
    if plan_only:
        jobs, errors = build_plan(profile, indexes, episode_orders)
        report.update(total_episodes=len(jobs), jobs=jobs, link_errors=errors)
        atomic_json(output, report)
        for error in errors:
            print(f"[链接 {error['index']}] 失败：{error['error']}", flush=True)
        print(f"清单共 {len(jobs)} 个去重后的正片任务。", flush=True)
        for job in jobs:
            video = job["video"]
            print(f"{video['title']} 第 {video['episode']} 集，上映 {video['release_date']}，quality={job['quality']}", flush=True)
        return report, output
    try:
        config = media_config(profile)
        checkpoint = partial(atomic_json, retry_settings=config["logging"])
        controls = StageControls(config)
        log_config = {**config, "output_path": resolve_path(main["output_path"], profile["main_config"].parent),
                      "work_dir": resolve_path(main["work_dir"], profile["main_config"].parent)}
        with output_lock(work_root, config["logging"]["lock_file"]), Journal(log_config, mode="playback") as journal:
            report["consolidated_records"] = consolidate_output(log_config["output_path"], config)
            print(f"实时下载与解析进度：{journal.url}", flush=True)
            if config["logging"]["open_browser"]:
                try:
                    if not webbrowser.open_new_tab(journal.url):
                        print("浏览器未自动打开，请访问上述本机地址。", flush=True)
                except Exception:
                    print("浏览器未自动打开，请访问上述本机地址。", flush=True)
            options = task_options(profile)
            registry = CancellationRegistry(options, indexes, controls.cancel, controls.interval)
            journal.cancellations = registry
            errors = report["link_errors"]
            with journal.lock:
                journal.run.update(catalog_status="discovering", catalog_errors=errors)
                journal.write()
            entries, task_stops, attempts = {}, {}, {}
            failed_entries, requested = {}, {}
            control_event = Event()
            stop_requested = False

            def update(item: dict, status: str, value) -> None:
                """在调度线程登记 item 的 status/value 并更新网页和批次记录；无返回值。"""
                entry = entries[id(item)]
                if status == "done":
                    item.update(value)
                    report["reused" if value["status"] == "reused" else "completed"] += 1
                    media = value.get("media", {})
                    media_profile = media.get("video_profile") or ""
                    detail = "文件名和大小核对通过，已复用" if value["status"] == "reused" else "媒体及复制校验通过"
                    media_label = entry.get("media_label", "")
                    if media:
                        media_label = f"{entry.get('quality_label', '')} · {media.get('width')}×{media.get('height')} · {media.get('video_codec')} {media_profile} · {media.get('pixel_format') or ''} · {', '.join(media.get('audio_codecs', []))} · {', '.join(value or '未知声道' for value in media.get('audio_layouts', []))}"
                    journal.update(entry, "completed", status=value["status"], progress=100.0, output=value["output"],
                                   detail=detail, media_label=media_label)
                    print(f"[{value['status']}] {value['output']}", flush=True)
                elif status == "failed":
                    item.update(status="failed", error=str(value))
                    report["failed"] += 1
                    with journal.lock:
                        failed_entries[entry["id"]] = item
                        journal.update(entry, status="failed", error=str(value), detail=str(value))
                    if hasattr(value, "report"):
                        item["source_diagnostic"] = value.report
                    print(f"[失败] {item['video']['title']} 第 {item['video']['episode']} 集：{value}", flush=True)
                elif status in ("interrupted", "cancelled"):
                    item["status"] = status
                    if status == "cancelled":
                        report["cancelled"] += 1
                    journal.update(entry, status=status, detail="已取消，可靠断点保留" if status == "cancelled" else "已停止，可靠断点保留")
                else:
                    item["status"] = status
                    journal.update(entry, status, detail={"waiting_download": "来源已就绪，等待下载名额", "waiting_queue": "下载完成，等待队列容量", "waiting_processing": "已下载，等待处理"}.get(status, "等待处理阶段资源"))
                checkpoint(output, report)

            def notify_for(entry):
                """为单集 entry 绑定线程安全进度回调；返回消费安全事件的函数。"""
                def notify(event):
                    """将当前集 event 的阶段、百分比和指标交给日志；无返回值。"""
                    journal.update(entry, **event)
                return notify

            def request_retry(entry_id: str) -> bool:
                """在日志锁内按当前失败行 entry_id 接受一次重试请求，返回是否已排队。"""
                item = failed_entries.get(entry_id)
                if stop_requested or item is None or entry_id in requested or task_stops[id(item)].is_set():
                    return False
                requested[entry_id] = item
                entries[id(item)]["retry_queued"] = True
                journal.run.pop("awaiting_stop", None)
                control_event.set()
                return True

            def request_stop(run_id: str) -> bool:
                """在日志锁内核对 run_id，只接受当前空闲批次的停止请求；返回是否已接受。"""
                nonlocal stop_requested
                if stop_requested or run_id != journal.run["id"] or not journal.run.get("awaiting_stop"):
                    return False
                stop_requested = True
                journal.run.pop("awaiting_stop", None)
                control_event.set()
                return True

            def retry_jobs(idle: bool) -> list:
                """返回供共享调度器派发的网页重试任务；idle 时等待重试或停止，停止时返回空列表。"""
                while True:
                    check_cancel(controls.cancel)
                    with journal.lock:
                        if stop_requested:
                            return []
                        if requested:
                            scheduled = []
                            for entry_id, item in requested.items():
                                entry, job = entries[id(item)], attempts[id(item)]
                                del failed_entries[entry_id]
                                job["_retry_from"] = job.get("_work_key")
                                item.pop("error", None)
                                item.pop("source_diagnostic", None)
                                item.update(status="pending", retries=item.get("retries", 0) + 1)
                                report["failed"] -= 1
                                journal.reset_episode(entry)
                                stopped = task_stops[id(item)]
                                scheduled.append((item, job_steps(profile, config, work_root, job, controls.for_task(stopped), notify_for(entry))))
                            requested.clear()
                            journal.write()
                            control_event.clear()
                            break
                        if not idle:
                            return []
                        if not journal.run.get("awaiting_stop"):
                            journal.run["awaiting_stop"] = True
                            journal.write()
                            print("本批次任务已处理完毕，网页服务继续运行。请在网页点击“停止服务”退出。", flush=True)
                    control_event.wait(controls.interval)
                checkpoint(output, report)
                return scheduled

            journal.retry_request, journal.stop_request = request_retry, request_stop

            def admit(batch):
                """在调度线程接纳目录 batch 并返回单集生成器列表；None 表示目录完成，保持最终总数真实。"""
                scheduled = []
                with journal.lock:
                    if batch is None:
                        journal.run["catalog_status"] = "ready"
                    else:
                        report["jobs"].extend(batch["jobs"])
                        errors.extend(batch["errors"])
                        report["total_episodes"] = len(report["jobs"])
                        journal.run.update(catalog_processed=batch["processed"], catalog_total=batch["total"],
                                           catalog_discovered=report["total_episodes"])
                        for error in batch["errors"]:
                            print(f"[链接 {error['index']}] 失败：{error['error']}", flush=True)
                        for job in batch["jobs"]:
                            stopped = registry.resolved(job["video"]["index"], job["video"])
                            item = {"video":job["video"], "quality":job["quality"], "status":"pending"}
                            task_stops[id(item)] = stopped
                            report["results"].append(item)
                            entry = journal.add_episode(job["video"], job["quality"], config["audio"]["quality"])
                            entries[id(item)] = entry
                            attempts[id(item)] = attempt = dict(job)
                            scheduled.append((item, job_steps(profile, config, work_root, attempt, controls.for_task(stopped), notify_for(entry))))
                    journal.write()
                checkpoint(output, report)
                return scheduled

            try:
                schedule([], config, controls, update,
                         discovery=plan_batches(profile, indexes, episode_orders, controls.cancel, options=options, registry=registry), admit=admit,
                         is_cancelled=lambda item: task_stops[id(item)].is_set(), retries=retry_jobs)
            finally:
                with journal.lock:
                    journal.retry_request = journal.stop_request = None
                    journal.run.pop("awaiting_stop", None)
                    if journal.run["catalog_status"] != "ready":
                        journal.run["catalog_status"] = "interrupted"
                        journal.write()
            journal.run["summary"] = {key: report[key] for key in ("completed", "reused", "failed", "cancelled")}
        report["status"] = "completed" if not report["failed"] and not errors else "failed"
        return report, output
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        raise
    except Exception as error:
        report.update(status="failed", batch_error=str(error))
        raise
    finally:
        atomic_json(output, report, retry_settings=main["logging"])
