"""保存已完成全片验证的候选，发布失败后按内容摘要复用，避免重复下载和解码。"""

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ..configuration import resolve_path
from ..process import probe_media
from ..storage import atomic_json, fingerprint, output_lock, publish, read_json, valid_digest
from .metadata import require_same_metadata
from .outputs import target_path


def resume_validated(work: Path, identity: dict, video: dict, config: dict, controls) -> dict | None:
    """核对 work 候选的 identity 及 video 命名资料；config/controls 控制指纹及取消，完整匹配返回检查点，候选缺失返回 None。"""
    checkpoint = work / "validated.json"
    candidate = work / "validated.partial.mp4"
    if not checkpoint.exists() or not candidate.exists():
        return None
    report = read_json(checkpoint)
    expected, verification = report.get("validated_fingerprint", {}), report.get("verification", {})
    if (report.get("record_type") != "playback_download" or report.get("schema_version") != 1
            or report.get("source_identity") != identity or report.get("media_verified") is not True
            or verification.get("decode_exit_code") != 0 or type(verification.get("frames")) is not int or verification["frames"] <= 0
            or not valid_digest(expected.get("sha256"))):
        raise RuntimeError("已验证候选缺少完整来源或解码证明，保留现场")
    saved_video = report.get("video", {})
    if any(saved_video.get(key) != video[key] for key in ("album_id", "title", "episode", "episode_title", "vid", "duration_seconds")):
        raise RuntimeError("已验证候选的官方命名或来源资料发生变化，不重新映射旧候选")
    # 日期字段引入前的已验证候选继续按原有来源和摘要复用，发布时绑定本次官方日期；不迁移历史文件。
    if "release_date" in saved_video and saved_video["release_date"] != video["release_date"]:
        raise RuntimeError("已验证候选的整剧上映日期发生变化，不重新映射旧候选")
    if not candidate.resolve().is_relative_to(work.resolve()) or not candidate.is_file():
        raise RuntimeError("已验证候选不是当前任务内的普通文件")
    actual = fingerprint(candidate, config["copy_buffer_bytes"], force=True, cancel_event=controls.cancel)
    if actual["sha256"] != expected["sha256"] or actual["size_bytes"] != expected["size_bytes"]:
        raise RuntimeError("已验证候选在全片校验后发生变化，保留现场")
    report.update(status="validated", resumed_validation=True)
    report.pop("error", None)
    report["media"] = probe_media(config, candidate, work / ("resume_media_" + uuid4().hex + ".log"), cancel_event=controls.cancel)
    if report["media"]["errors"]:
        raise RuntimeError("已验证候选的实际媒体属性探测失败，保留现场")
    return report


def publish_validated(profile: dict, config: dict, video: dict, source: dict, work: Path, report: dict, controls, notify=None) -> dict:
    """将 work/report 的已验证候选发布到 profile/config 输出目录；video/source 核对命名，controls/notify 限制及上报阶段；返回成品报告。"""
    candidate = work / "validated.partial.mp4"
    try:
        require_same_metadata(profile, video)
        root = resolve_path(config["output_path"], profile["main_config"].parent)
        if notify:
            notify({"stage": "publishing", "detail": "获取输出目录写入权并保存成品"})
        with controls.slot("publish"), output_lock(root):
            target = target_path(config, profile["main_config"], video, source)
            if target.exists() or target.with_suffix(".playback.json").exists():
                target = target.with_stem(target.stem + "_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f"))
            if not target.is_relative_to(root):
                raise ValueError("播放成品路径超出主配置输出目录")
            report["status"] = "publishing"
            report["output_fingerprint"] = publish(candidate, target, report["validated_fingerprint"]["sha256"], config["copy_buffer_bytes"], cancel_event=controls.cancel)
            report.update(output=str(target), status="completed")
            atomic_json(target.with_suffix(".playback.json"), report, retry_settings=config["logging"], cancel_event=controls.cancel)
        if not config["keep_intermediates"]:
            paths = [candidate, work / "outer.ts", work / "clear.ts", work / "highest_audio.m4a", *work.glob("clear_*.mp4"), *work.glob("segment_*.ts")]
            warnings = []
            for path in paths:
                if path.exists():
                    if not path.resolve().is_relative_to(work.resolve()) or not path.is_file():
                        warnings.append("中间媒体路径无效，保留：" + path.name)
                        continue
                    try:
                        path.unlink()
                    except OSError as error:
                        warnings.append(f"中间媒体清理失败：{path.name}，{type(error).__name__}")
            if warnings:
                report["cleanup_warnings"] = warnings
        return report
    except BaseException as error:
        report.update(status="interrupted" if isinstance(error, (KeyboardInterrupt, GeneratorExit)) or controls.cancel.is_set() else "failed", error=str(error))
        raise
    finally:
        atomic_json(work / "full_report.json", report, retry_settings=config["logging"])
