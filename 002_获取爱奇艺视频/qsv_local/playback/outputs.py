"""链接模式的输出映射及经过内容指纹复核的成品复用，与 QSV 来源索引独立。"""

from pathlib import Path
import re
from uuid import uuid4

from ..configuration import resolve_path
from ..naming import output_name
from ..process import probe_media
from ..storage import fingerprint, read_json, valid_digest
from .metadata import require_same_metadata
from .naming import dated_folder


def target_path(config: dict, main_path: Path, video: dict, source: dict, *, with_date: bool = True) -> Path:
    """根据 config/main_path 与 video/source 生成安全成品路径；with_date 为真时添加日期，假值仅用于查找已登记的历史无日期目录。"""
    root = resolve_path(config["output_path"], main_path.parent)
    folder, name = output_name(config["naming"], (video["album_id"], video["title"], str(video["episode"])),
                               video["episode_title"] + "_" + source["scrsz"])
    if with_date:
        folder = dated_folder(config["naming"], folder, video.get("release_date"))
    target = (root / folder / (name + ".mp4")).resolve()
    if not target.is_relative_to(root):
        raise ValueError("播放成品路径超出主配置输出目录")
    return target


def reusable_output(profile: dict, config: dict, video: dict, source: dict, identity: dict, selection: dict) -> dict | None:
    """按 profile/config 核对 video/source 的 identity 成品及 selection 证明；日期目录优先，旧目录仅校验复用、不迁移，返回实际原路径。"""
    preferred = target_path(config, profile["main_config"], video, source)
    legacy = target_path(config, profile["main_config"], video, source, with_date=False)
    bases = [preferred] if preferred == legacy else [preferred, legacy]
    suffix = ".playback.json"
    candidates = []
    for base in bases:
        if base.parent.is_dir():
            name_pattern = re.compile(re.escape(base.stem) + r"(?:_\d{8}_\d{6}_\d{6})?\.mp4")
            candidates.extend((base, name_pattern, path) for path in sorted(base.parent.glob("*" + suffix), key=lambda path: path.stat().st_mtime_ns, reverse=True))
    for base, name_pattern, record_path in candidates:
        candidate = record_path.with_name(record_path.name[:-len(suffix)] + ".mp4")
        if not name_pattern.fullmatch(candidate.name):
            continue
        data = read_json(record_path)
        if data.get("record_type") != "playback_download":
            continue
        if data.get("schema_version") != 1:
            raise ValueError("链接成品记录版本不受支持，保留原文件")
        if data.get("source_identity") != identity or data.get("status") != "completed":
            continue
        saved_video = data.get("video", {})
        if "release_date" in saved_video and saved_video["release_date"] != video["release_date"]:
            raise ValueError("已登记成品的整剧上映日期发生变化，保留原目录并停止映射")
        verification, expected = data.get("verification", {}), data.get("output_fingerprint", {})
        if (data.get("media_verified") is not True or verification.get("decode_exit_code") != 0
                or type(verification.get("frames")) is not int or verification["frames"] <= 0
                or not valid_digest(expected.get("sha256"))):
            raise ValueError("链接成品记录缺少完整验证证明")
        audio_selection = source.get("_audio_selection")
        if audio_selection and (data.get("audio_selection") != audio_selection or not data.get("audio_media") or audio_selection["rank"] == 1 and data.get("highest_audio_verified") is not True):
            continue
        if not candidate.resolve().is_relative_to(base.parent) or Path(data["output"]).resolve() != candidate.resolve():
            raise ValueError("链接成品记录路径越界或与文件不一致")
        if not candidate.is_file():
            continue
        actual = fingerprint(candidate, config["copy_buffer_bytes"], force=True)
        if actual["sha256"] != expected["sha256"] or actual["size_bytes"] != expected["size_bytes"]:
            continue
        log = resolve_path(config["work_dir"], profile["main_config"].parent) / "playback_downloads" / "reuse_checks" / (uuid4().hex + ".log")
        log.parent.mkdir(parents=True, exist_ok=True)
        media = probe_media(config, candidate, log)
        if (media["errors"] or f"{media['width']}x{media['height']}" != source["scrsz"]
                or abs(media["duration_seconds"] - video["duration_seconds"]) > profile["playback"]["media_download"]["duration_tolerance_seconds"]):
            raise ValueError("既有成品实际媒体属性与当前来源不一致")
        if audio_selection and (media["audio_codecs"] != data["audio_media"]["audio_codecs"] or media["audio_layouts"] != data["audio_media"]["audio_layouts"]):
            raise ValueError("既有成品音轨与已验证的所选独立音轨不一致")
        require_same_metadata(profile, video)
        return {"status": "reused", "output": str(candidate), "output_fingerprint": actual,
                "media_verified": True, "verification": verification, "media": media, "selection": selection,
                "highest_quality_verified": selection["rank"] == 1 and selection["native_selection_verified"], "source_identity": identity,
                "audio_selection": audio_selection, "audio_media": data.get("audio_media"), "highest_audio_verified": data.get("highest_audio_verified", False),
                "release_date": video["release_date"], "legacy_directory": base.parent != preferred.parent,
                "download_tasks_created": False, "visible_player_switched": False}
    return None
