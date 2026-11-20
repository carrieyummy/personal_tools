"""为链接下载生成上映日期目录前缀及统一的画质标签。"""

from datetime import date
from string import Formatter

from ..naming import safe_component, validate_component


def quality_label(source: dict) -> str:
    """按已选原生视频 source 的帧绮标记和 HDR 类型返回网页及文件名共用的画质标签。"""
    if source.get("zqyh") == 1:
        return "帧绮映画"
    return "增强画质" if source["dr"] > 0 else "标准画质"


def validate_date_prefix(naming: dict) -> None:
    """校验 naming 中链接专用前缀模板；必须使用 release_date 日期字段并生成安全单层目录，无返回值。"""
    template = naming.get("playback_date_prefix")
    if not isinstance(template, str) or not template:
        raise ValueError("naming.playback_date_prefix 必须为非空日期前缀模板")
    try:
        fields = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
        if fields != {"release_date"}:
            raise ValueError("日期前缀必须包含且仅包含 release_date")
        validate_component(template.format(release_date=date(2000, 1, 2)) + "标题")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
        raise ValueError("naming.playback_date_prefix 不能生成有效日期目录") from error


def movie_name(naming: dict, video: dict, media: str) -> str:
    """根据 naming 的电影模板、video 资料和 media 描述生成安全电影文件名，不含扩展名。"""
    template = naming.get("movie_template")
    try:
        if not isinstance(template, str) or not template:
            raise ValueError("缺少电影模板")
        fields = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
        if fields != {"release_date", "title", "series_id", "media"}:
            raise ValueError("电影模板字段无效")
        day = date.fromisoformat(video["release_date"])
        if day.isoformat() != video["release_date"]:
            raise ValueError("电影日期格式无效")
        series_id = video["album_id"]
        if not isinstance(series_id, str) or not series_id.isascii() or not series_id.isdecimal() or int(series_id) <= 0:
            raise ValueError("电影编号无效")
        return validate_component(template.format(release_date=day, title=safe_component(video["title"]),
                                                  series_id=series_id, media=safe_component(media)))
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
        raise ValueError("naming.movie_template 或电影上映日期无效") from error


def dated_folder(naming: dict, folder: str, release_date: str) -> str:
    """将 naming 前缀与原 folder 合并；release_date 必须为有效 ISO 年月日字符串，返回安全目录名。"""
    try:
        day = date.fromisoformat(release_date)
        if day.isoformat() != release_date:
            raise ValueError("非规范日期")
    except (TypeError, ValueError):
        raise ValueError("链接剧集缺少有效的整剧上映日期") from None
    prefix = naming["playback_date_prefix"].format(release_date=day)
    return validate_component(prefix + folder)
