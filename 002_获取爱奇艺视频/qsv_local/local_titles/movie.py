"""使用客户端下载记录为本地电影生成输出文件名。"""

from datetime import datetime
from pathlib import Path
import re

from ..playback.naming import movie_name


def movie_target(naming: dict, metadata: tuple, source: Path, output: Path) -> Path:
    """按 naming、电影 metadata 和 source 映射到 output 根目录；返回绝对 MP4 目标路径。"""
    album_id, title, order, channel, content_type, release = metadata
    if (channel != "1" or content_type != "1" or not isinstance(order, str)
            or not order.isascii() or not order.isdecimal()):
        raise ValueError(f"电影下载记录的频道、正片类型或集序无效：{source.name}")
    if not isinstance(release, str) or re.fullmatch(r"[0-9]{8}", release) is None:
        raise ValueError(f"电影下载记录缺少有效上映日期：{source.name}")
    try:
        release_date = datetime.strptime(release, "%Y%m%d").date().isoformat()
    except ValueError:
        raise ValueError(f"电影下载记录上映日期无效：{source.name}") from None
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"电影下载记录缺少片名：{source.name}")
    media = source.stem
    if media.startswith(title) and media[len(title):].lstrip("-_ "):
        media = media[len(title):].lstrip("-_ ")
    video = {"album_id": album_id, "title": title, "release_date": release_date}
    return (output / (movie_name(naming, video, media) + ".mp4")).resolve()
