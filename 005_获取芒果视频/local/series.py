"""从芒果专辑页面读取整剧上映日期，生成可配置的安全目录前缀。"""

from datetime import date
import re
from typing import Any
from urllib.parse import urlsplit

from .errors import ToolError


def read_release_date(transport, settings: dict[str, Any], clip_id: str) -> str:
    """使用 transport 按 settings 读取数字 clip_id 的官方专辑页面；返回唯一有效的 YYYY-MM-DD 日期。"""
    if not isinstance(clip_id, str) or not clip_id.isascii() or not clip_id.isdecimal() or int(clip_id) <= 0:
        raise ToolError("剧集编号无效，不能获取上映日期")
    expected = urlsplit(settings["page_url"].format_map({"clip_id": clip_id}))
    with transport.open(expected.geturl()) as response:
        actual = urlsplit(response.geturl())
        if actual.scheme != expected.scheme or actual.hostname != expected.hostname or actual.path != expected.path:
            raise ToolError("专辑页面跳转后与目标剧集不一致")
        maximum = settings["max_page_bytes"]
        raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise ToolError("专辑页面超过配置的响应上限")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ToolError("专辑页面不是有效 UTF-8") from None
    field = re.escape(settings["release_date_field"])
    matches = re.findall(r'(?<![\w$])(?:["\']?' + field + r'["\']?)\s*:\s*["\'](\d{4}-\d{2}-\d{2})(?:\s[^"\']*)?["\']', content)
    if len(set(matches)) != 1:
        raise ToolError("专辑页面缺少唯一上映日期，不使用其他日期猜测")
    try:
        return date.fromisoformat(matches[0]).isoformat()
    except ValueError:
        raise ToolError("专辑上映日期无效") from None


def date_prefix(naming: dict[str, Any], release_date: str) -> str:
    """按 naming 的前缀模板格式化 release_date；日期须为有效 YYYY-MM-DD，返回安全目录前缀。"""
    try:
        if date.fromisoformat(release_date).isoformat() != release_date:
            raise ValueError
    except (TypeError, ValueError):
        raise ToolError("电视剧缺少有效上映日期，不能生成目录") from None
    return naming["series_date_prefix"].format_map({"release_date": release_date})
