"""链接模式的输出映射及成品复用，从共用目录索引读取独立的链接来源记录。"""

from pathlib import Path
import re
from stat import S_ISREG

from ..configuration import resolve_path
from ..naming import output_name
from .metadata import require_same_metadata
from .naming import dated_folder, movie_name, quality_label
from .records import PlaybackDirectoryIndex


def output_names(naming: dict, video: dict, source: dict, *, with_quality: bool = True, with_language: bool = True) -> tuple[str, str]:
    """用 naming 模板和 video/source 生成目录及文件主名；with_quality、with_language 控制画质及配音标签。"""
    title = video["episode_title"]
    if with_quality:
        title += "_" + quality_label(source)
    title += "_" + source["scrsz"]
    language = source.get("_audio_language")
    if with_language and isinstance(language, str) and language.strip():
        title += "_" + language + "音轨"
    return output_name(naming, (video["album_id"], video["title"], str(video["episode"])), title)


def target_path(config: dict, main_path: Path, video: dict, source: dict, *,
                with_date: bool = True, with_quality: bool = True) -> Path:
    """按 config/main_path 和 video/source 返回安全成品路径；with_date、with_quality 控制日期和画质标签，假值用于匹配旧成品。"""
    root = resolve_path(config["output_path"], main_path.parent)
    if video.get("is_movie") is True:
        media = quality_label(source) + "_" + source["scrsz"] if with_quality else source["scrsz"]
        language = source.get("_audio_language")
        if isinstance(language, str) and language.strip():
            media += "_" + language + "音轨"
        target = (root / (movie_name(config["naming"], video, media) + ".mp4")).resolve()
        if target.parent != root:
            raise ValueError("播放成品路径超出主配置输出目录")
        return target
    folder, name = output_names(config["naming"], video, source, with_quality=with_quality)
    if with_date:
        folder = dated_folder(config["naming"], folder, video.get("release_date"))
    target = (root / folder / (name + ".mp4")).resolve()
    if not target.is_relative_to(root):
        raise ValueError("播放成品路径超出主配置输出目录")
    return target


def reusable_output(profile: dict, config: dict, video: dict, source: dict, identity: dict, selection: dict) -> dict | None:
    """按 profile/config 与 video/source 定位成品，核对 identity 对应的成功记录及当前文件名和大小。

    selection 为本次选档证明。日期目录优先，同目录优先带标签名称，也认可已登记的旧名称；匹配返回复用报告，否则返回 None。
    """
    preferred = target_path(config, profile["main_config"], video, source)
    legacy = target_path(config, profile["main_config"], video, source, with_date=False)
    _, quality_stem = output_names(config["naming"], video, source, with_language=False)
    _, old_stem = output_names(config["naming"], video, source, with_quality=False, with_language=False)
    patterns = [re.compile(re.escape(stem) + r"(?:_\d{8}_\d{6}_\d{6})*\.mp4") for stem in (preferred.stem, quality_stem, old_stem)]
    folders = [preferred.parent, legacy.parent]
    if video.get("is_movie") is True:
        old_folder, old_language_stem = output_names(config["naming"], video, source)
        root_folder = preferred.parent
        folders.extend((root_folder / dated_folder(config["naming"], old_folder, video["release_date"]), root_folder / old_folder))
        patterns.append(re.compile(re.escape(old_language_stem) + r"(?:_\d{8}_\d{6}_\d{6})*\.mp4"))
        if source.get("_audio_language"):
            without_language = {**source, "_audio_language": None}
            no_language = target_path(config, profile["main_config"], video, without_language)
            patterns.append(re.compile(re.escape(no_language.stem) + r"(?:_\d{8}_\d{6}_\d{6})*\.mp4"))
    root = resolve_path(config["output_path"], profile["main_config"].parent)
    candidates = []
    for folder in dict.fromkeys(folders):
        if folder.is_dir():
            records = PlaybackDirectoryIndex(folder, root).records()
            for pattern in patterns:
                candidates.extend((folder / name, data) for name, data in reversed(records.items()) if pattern.fullmatch(name))
    for candidate, data in candidates:
        if data.get("source_identity") != identity or data.get("status") != "completed":
            continue
        saved_video = data.get("video", {})
        if "release_date" in saved_video and saved_video["release_date"] != video["release_date"]:
            raise ValueError("已登记成品的整剧上映日期发生变化，保留原目录并停止映射")
        verification, expected = data["verification"], data["output_fingerprint"]
        audio_selection = source.get("_audio_selection")
        if audio_selection and (data.get("audio_selection") != audio_selection or not data.get("audio_media") or audio_selection["rank"] == 1 and data.get("highest_audio_verified") is not True):
            continue
        try:
            info = candidate.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        if not S_ISREG(info.st_mode) or info.st_size != expected["size_bytes"]:
            continue
        require_same_metadata(profile, video)
        # 摘要、媒体属性及完整验证证明均取自发布记录，本次不读取成品内容。
        return {"status": "reused", "reuse_check": "filename_and_size", "output": str(candidate), "output_fingerprint": expected,
                "media_verified": True, "verification": verification, "media": data.get("media") or {}, "selection": selection,
                "highest_quality_verified": selection["rank"] == 1 and selection["native_selection_verified"], "source_identity": identity,
                "audio_selection": audio_selection, "audio_media": data.get("audio_media"), "highest_audio_verified": data.get("highest_audio_verified", False),
                "release_date": video["release_date"], "legacy_directory": candidate.parent != preferred.parent,
                "download_tasks_created": False, "visible_player_switched": False}
    return None
