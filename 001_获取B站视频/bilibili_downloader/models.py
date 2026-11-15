"""定义分 P、字幕和画质的共用数据结构及画质标识，不依赖认证、下载或记录持久化。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import ToolError


@dataclass(frozen=True)
class VideoTarget:
    """描述单个下载目标；url/cid 为规范地址及内容ID，part/total_parts 为所属来源的序号和总数。

    番剧填写 source_id、episode_id、season_id、真实 bvid/aid 和 title；metadata 为同来源的完整原站信息。
    普通投稿的新增字段可为空，由原有投稿接口取得身份，不改变原位置参数的含义。
    """

    url: str
    cid: int
    part: int
    total_parts: int = 1
    source_id: str = ''
    episode_id: int | None = None
    season_id: int | None = None
    bvid: str | None = None
    aid: int | None = None
    title: str = ''
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class SubtitleTrack:
    """描述平台字幕：language 为语言码，name 为显示名，is_ai 标记 AI，url 为源地址。"""

    language: str
    name: str
    is_ai: bool
    url: str
    track_id: str


@dataclass(frozen=True)
class Quality:
    """平台画质档位：code 为正整数编号（None 表示旧版未确认），name 为平台名称。"""

    code: int | None
    name: str

    @property
    def directory(self) -> str:
        """返回用于目录或视频文件名的安全实际画质后缀；平台编号区分同名档位。"""
        if self.code is None:
            return "旧版_未确认清晰度"
        label = re.sub(r'[\s<>:"/\\|?*\x00-\x1f]+', '_', self.name).strip('._')[:60]
        return f"qn{self.code}_{label or self.code}"


LEGACY_QUALITY = Quality(None, "旧版_未确认清晰度")


def quality_entry_id(source_id: str, quality: Quality) -> str:
    """为 source_id 原视频/拼接标识追加 quality 档位；旧条目保持原标识，返回唯一字符串。"""
    return source_id if quality.code is None else f"{source_id}:qn{quality.code}"


def entry_quality(item: dict[str, Any]) -> Quality:
    """读取 item 的明确平台档位；无编号时返回旧版未确认档位，不由尺寸或格式编号推断。"""
    code = item.get("quality_id")
    if code is None:
        codes = {fmt.get("quality") for fmt in item.get("formats", []) if type(fmt.get("quality")) is int and fmt["quality"] > 0}
        code = next(iter(codes)) if len(codes) == 1 else None
    if code is None:
        return LEGACY_QUALITY
    if type(code) is not int or code < 1:
        raise ToolError("下载记录中的清晰度编号无效")
    name = (item.get("quality_name") if item.get("quality_id") == code else None) or next((fmt.get("format_note") or fmt.get("format") for fmt in item.get("formats", []) if fmt.get("quality") == code), None)
    return Quality(code, name or f"画质{code}")
