"""在共用目录名上追加链接模式的上映日期前缀，视频文件名继续使用原模板。"""

from datetime import date
from string import Formatter

from ..naming import validate_component


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
