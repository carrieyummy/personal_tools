"""验证爱奇艺电影正片的公开资料，可选下载并完整解码到研究目录。"""

import argparse
from datetime import datetime
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.playback import download
from qsv_local.playback.configuration import read_profile
from qsv_local.playback.listing import task_options
from qsv_local.playback.media import media_config
from qsv_local.playback.metadata import resolve_video_link
from qsv_local.playback.naming import movie_name
from qsv_local.playback.source import select_av_source
from qsv_local.runtime import check_cancel
from qsv_local.storage import atomic_json


def resolve_movie(profile: dict, url: str, index: int) -> tuple[dict, dict]:
    """用正式解析规则核对 profile 中下标 index 的电影 url，返回任务资料及公开证据。"""
    video, evidence = resolve_video_link(profile, url, index)
    if video.get("is_movie") is not True:
        raise ValueError("官方节目资料未确认目标为有效的独立电影正片")
    return video, evidence


def verify_media(profile: dict, task: dict, video: dict, work: Path) -> tuple[dict, dict]:
    """按 profile 下 task 的画质和主配置音质验证 video；候选留在 work，返回选档与全片报告。"""
    config = media_config(profile)
    source = select_av_source(profile, video, task["quality"], config["audio"]["quality"])

    def retain_candidate(active_profile, _config, active_video, _source, active_work, report, controls, _notify=None):
        """核对本次 active_profile、active_video 和 active_work 下的候选，返回含研究文件路径的 report。

        controls 提供取消状态；_config、_source 和 _notify 仅用于匹配被替换的发布函数签名。
        """
        check_cancel(controls.cancel)
        candidate = work / "validated.partial.mp4"
        if active_profile is not profile or active_work != work or active_video != video:
            raise RuntimeError("待验证候选不属于本次研究任务")
        current, _ = resolve_movie(profile, task["url"], task["index"])
        if current != video:
            raise RuntimeError("全片验证期间电影身份、时长或上映日期发生变化")
        if report.get("media_verified") is not True or not candidate.is_file() or candidate.resolve() != candidate:
            raise RuntimeError("未取得本次研究目录内的有效全片候选")
        return {**report, "output": str(candidate), "candidate_only": True}

    # 研究入口在独立进程中替换最终发布回调；完整媒体验证仍由正式下载器执行。
    with patch.object(download, "publish_validated", retain_candidate):
        report = download.download_full(profile, video, source.selected, source.manifest, work, source.headers,
                                        source.report["selection"])
    return source.report, report


def main() -> int:
    """解析研究配置、清单下标和验证模式；成功、失败和中断分别返回 0、1 和 130。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="现有播放专项 YAML 路径")
    parser.add_argument("--index", type=int, required=True, help="下载清单下标，从 0 开始")
    parser.add_argument("--full-media", action="store_true", help="验证授权正片全片，候选仅保留在研究目录")
    args = parser.parse_args()
    if args.index < 0:
        parser.error("--index 必须为非负整数")
    output = None
    report = {"mode": "full_media" if args.full_media else "public_metadata", "status": "resolving"}
    try:
        profile = read_profile(args.config)
        tasks = task_options(profile)
        if args.index >= len(tasks):
            raise ValueError("链接下标超出当前清单范围")
        task = tasks[args.index]
        output = profile["report_dir"] / (datetime.now().astimezone().strftime("movie_%Y%m%d_%H%M%S_%f") + ".json")
        video, evidence = resolve_movie(profile, task["url"], task["index"])
        naming = profile["_naming_snapshot"]
        preview = movie_name(naming, video, "媒体信息待选档") + ".mp4"
        report.update(status="metadata_verified", video=video, public_evidence=evidence, filename_preview=preview)
        atomic_json(output, report)
        print(f"电影公开资料通过：{video['title']}，TVID {video['tvid']}，{video['duration_seconds']} 秒，院线上映日 {video['release_date']}", flush=True)
        print(f"文件名预览：{preview}", flush=True)
        print(f"电影研究报告：{output}", flush=True)
        if args.full_media:
            print("正在请求所选画质和音轨的完整授权播放源。", flush=True)
            selection, full = verify_media(profile, task, video, output.with_suffix(""))
            report.update(status="media_verified", selection=selection, full_media=full)
            atomic_json(output, report)
            print(f"全片验证通过：{full['output']}", flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        interrupted = isinstance(error, KeyboardInterrupt)
        report.update(status="interrupted" if interrupted else "failed", error=str(error) or type(error).__name__)
        if output is not None:
            atomic_json(output, report)
            print(f"电影研究报告：{output}", flush=True)
        print(f"电影研究退出：{report['error']}", file=sys.stderr, flush=True)
        return 130 if interrupted else 1


if __name__ == "__main__":
    raise SystemExit(main())
