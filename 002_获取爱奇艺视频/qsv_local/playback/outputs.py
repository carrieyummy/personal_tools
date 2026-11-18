"""链接模式的输出映射及成品复用，从共用目录索引读取独立的链接来源记录。"""

from pathlib import Path
import re
from stat import S_ISREG

from ..configuration import resolve_path
from ..naming import output_name
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
    """按 profile/config 与 video/source 定位成品，核对 identity 对应的成功记录及当前文件名和大小。

    selection 为本次选档证明。日期目录优先，匹配后返回复用报告，其中媒体属性和指纹来自发布记录；未匹配返回 None。
    """
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
                "release_date": video["release_date"], "legacy_directory": base.parent != preferred.parent,
                "download_tasks_created": False, "visible_player_switched": False}
    return None
