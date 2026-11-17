"""链接模式的输出映射及成品复用，从共用目录索引读取独立的链接来源记录。"""

from pathlib import Path
import re
from uuid import uuid4

from ..configuration import resolve_path
from ..naming import output_name
from ..process import probe_media
from ..storage import fingerprint
from .metadata import require_same_metadata
from .naming import dated_folder
from .records import PlaybackDirectoryIndex


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
    """从共用目录索引核对 profile/config、video/source、identity 及 selection；日期目录优先，旧目录不迁移，返回实际成品报告或 None。"""
    preferred = target_path(config, profile["main_config"], video, source)
    legacy = target_path(config, profile["main_config"], video, source, with_date=False)
    bases = [preferred] if preferred == legacy else [preferred, legacy]
    root = resolve_path(config["output_path"], profile["main_config"].parent)
    candidates = []
    for base in bases:
        if base.parent.is_dir():
            name_pattern = re.compile(re.escape(base.stem) + r"(?:_\d{8}_\d{6}_\d{6})?\.mp4")
            index = PlaybackDirectoryIndex(base.parent, root)
            candidates.extend((base, name_pattern, name, data) for name, data in reversed(index.records().items()))
    for base, name_pattern, name, data in candidates:
        candidate = base.parent / name
        if not name_pattern.fullmatch(name):
            continue
        if data.get("source_identity") != identity or data.get("status") != "completed":
            continue
        saved_video = data.get("video", {})
        if "release_date" in saved_video and saved_video["release_date"] != video["release_date"]:
            raise ValueError("已登记成品的整剧上映日期发生变化，保留原目录并停止映射")
        verification, expected = data["verification"], data["output_fingerprint"]
        audio_selection = source.get("_audio_selection")
        if audio_selection and (data.get("audio_selection") != audio_selection or not data.get("audio_media") or audio_selection["rank"] == 1 and data.get("highest_audio_verified") is not True):
            continue
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
