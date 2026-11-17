"""按 YAML 清单顺序获取链接视频，原生会话仅初始化一次；单项失败留现场并继续其余任务。"""

from datetime import datetime
from functools import partial
from pathlib import Path

import yaml
import webbrowser

from ..configuration import resolve_path
from ..activity_log import Journal
from ..storage import atomic_json, output_lock
from .configuration import read_profile
from .metadata import linked_videos, require_same_metadata
from .coordination import StageControls
from .download import download_steps, manifest_identity
from .listing import expand_episodes, task_options
from .media import media_config
from .manifests import selected_media_plan
from .outputs import reusable_output
from .source import select_av_source
from .scheduler import schedule


def build_plan(profile: dict, indexes: list[int] | None, episode_orders: list[int] | None = None) -> tuple[list[dict], list[dict]]:
    """展开 profile 中 indexes 指定的链接；episode_orders 可过滤展开后的正整数集序；返回去重作业及逐链接错误。"""
    options = task_options(profile)
    chosen = list(range(len(options))) if indexes is None else indexes
    if not chosen or len(set(chosen)) != len(chosen) or any(type(value) is not int or value < 0 or value >= len(options) for value in chosen):
        raise ValueError("链接下标为空、重复或超出当前清单")
    if episode_orders is not None and (not episode_orders or len(set(episode_orders)) != len(episode_orders)
                                      or any(type(value) is not int or value <= 0 for value in episode_orders)):
        raise ValueError("集序过滤必须为不重复的正整数列表")
    jobs, errors, identities = [], [], set()
    for index in chosen:
        option = options[index]
        try:
            link = linked_videos(profile, [index])[0]
            episodes = expand_episodes(profile, link, option["all_parts"])
            if episode_orders is not None:
                if not set(episode_orders) <= {video["episode"] for video in episodes}:
                    raise ValueError("当前任务按 all-parts 展开后不含所指定的集序")
                episodes = [video for video in episodes if video["episode"] in episode_orders]
            for video in episodes:
                key = (video["tvid"], option["quality"])
                if key not in identities:
                    identities.add(key)
                    jobs.append({"video": video, "quality": option["quality"], "note": option["note"]})
        except Exception as error:
            errors.append({"index": index, "stage": "resolving_link", "error": str(error)})
    return jobs, errors


def job_steps(profile: dict, config: dict, work_root: Path, job: dict, controls: StageControls, progress=None):
    """解析 job 并获取资源；profile/config 控制媒体，work_root 保存断点，controls 限制并发，progress 接收单集事件；让出字节数并返回结果。"""
    video = job["video"]
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
    with controls.identity(key):
        if progress:
            progress({"stage": "checking_reuse", "detail": "核对既有成品来源和内容摘要"})
        completed = reusable_output(profile, config, video, source, identity, selection)
        if completed is None:
            work = work_root / key
            completed = yield from download_steps(profile, video, source, result.manifest, work, result.headers, selection, controls, progress)
    return completed


def run_downloads(path: Path, indexes: list[int] | None = None, plan_only: bool = False,
                  episode_orders: list[int] | None = None) -> tuple[dict, Path]:
    """按 path 处理 indexes 链接，episode_orders 可过滤展开后的集序；plan_only 只查询公开目录；返回安全批次报告及路径。"""
    profile = read_profile(path)
    main = yaml.safe_load(profile["main_config"].read_text(encoding="utf-8-sig"))
    work_root = resolve_path(main["work_dir"], profile["main_config"].parent) / "playback_downloads"
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    output = work_root / ("plan_" if plan_only else "batch_")
    output = output.with_name(output.name + stamp + ".json")
    report = {"mode": "plan" if plan_only else "native_program_download", "total_episodes": 0, "completed": 0,
              "reused": 0, "failed": 0, "link_errors": [], "jobs": [], "results": [], "status": "planned" if plan_only else "running",
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
        with output_lock(work_root), Journal(log_config, mode="playback") as journal:
            print(f"实时下载与解析进度：{journal.url}", flush=True)
            if config["logging"]["open_browser"]:
                try:
                    if not webbrowser.open_new_tab(journal.url):
                        print("浏览器未自动打开，请访问上述本机地址。", flush=True)
                except Exception:
                    print("浏览器未自动打开，请访问上述本机地址。", flush=True)
            jobs, errors = build_plan(profile, indexes, episode_orders)
            report.update(total_episodes=len(jobs), jobs=jobs, link_errors=errors)
            with journal.lock:
                journal.run.update(catalog_status="ready", catalog_errors=errors)
                journal.write()
            for error in errors:
                print(f"[链接 {error['index']}] 失败：{error['error']}", flush=True)
            print(f"清单共 {len(jobs)} 个去重后的正片任务。", flush=True)
            entries = {}

            def update(item: dict, status: str, value) -> None:
                """在调度线程登记 item 的 status/value 并更新网页和批次记录；无返回值。"""
                entry = entries[id(item)]
                if status == "done":
                    item.update(value)
                    report["reused" if value["status"] == "reused" else "completed"] += 1
                    media = value.get("media", {})
                    media_profile = media.get("video_profile") or ""
                    journal.update(entry, "completed", status=value["status"], progress=100.0, output=value["output"], detail="媒体及复制校验通过",
                                   media_label=f"{entry.get('quality_label', '')} · {media.get('width')}×{media.get('height')} · {media.get('video_codec')} {media_profile} · {media.get('pixel_format') or ''} · {', '.join(media.get('audio_codecs', []))} · {', '.join(value or '未知声道' for value in media.get('audio_layouts', []))}")
                    print(f"[{value['status']}] {value['output']}", flush=True)
                elif status == "failed":
                    item.update(status="failed", error=str(value))
                    report["failed"] += 1
                    journal.update(entry, status="failed", error=str(value), detail=str(value))
                    if hasattr(value, "report"):
                        item["source_diagnostic"] = value.report
                    print(f"[失败] {item['video']['title']} 第 {item['video']['episode']} 集：{value}", flush=True)
                elif status == "interrupted":
                    item["status"] = status
                    journal.update(entry, status="interrupted", detail="已停止，可靠断点保留")
                else:
                    item["status"] = status
                    journal.update(entry, status, detail="等待解析阶段资源")
                checkpoint(output, report)

            def notify_for(entry):
                """为单集 entry 绑定线程安全进度回调；返回消费安全事件的函数。"""
                def notify(event):
                    """将当前集 event 的阶段、百分比和指标交给日志；无返回值。"""
                    journal.update(entry, **event)
                return notify

            scheduled = []
            for job in jobs:
                item = {"video": job["video"], "quality": job["quality"], "status": "pending"}
                report["results"].append(item)
                entry = journal.add_episode(job["video"], job["quality"], config["audio"]["quality"])
                entries[id(item)] = entry
                scheduled.append((item, job_steps(profile, config, work_root, job, controls, notify_for(entry))))
            checkpoint(output, report)
            schedule(scheduled, config, controls, update)
            journal.run["summary"] = {key: report[key] for key in ("completed", "reused", "failed")}
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
