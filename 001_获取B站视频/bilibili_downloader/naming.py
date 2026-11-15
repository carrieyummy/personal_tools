"""统一清理文件名并生成投稿及视频路径，路径参数由配置提供。"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Settings
from .errors import ToolError


def safe_name(value: str, limit: int) -> str:
    """将 value 转为 Windows 安全文件名，最多 limit 个字符；保留中文并处理设备保留名。"""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:limit].rstrip(" .") or "video"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", value.split(".")[0], re.I):
        value = "_" + value
    return value


def video_stem(settings: Settings, title: str, video_id: str) -> str:
    """按 settings 的长度限制清理 title，以 _ 连接来源或分P标识 video_id，返回无扩展名的名称。"""
    return f"{safe_name(title, settings.max_title_length)}_{safe_name(video_id, 60)}"


def video_folder(settings: Settings, title: str, video_id: str) -> Path:
    """根据 settings、来源 title 和 BV/ss/ep 标识 video_id 返回“标题_来源标识”目录路径。"""
    return settings.output_dir / video_stem(settings, title, video_id)


def quality_stem(base_stem: str, suffix: str, mix_quality: bool) -> str:
    """按 mix_quality 为基础文件名 base_stem 追加实际画质 suffix，返回不截断标识的无扩展名名称。"""
    return f'{base_stem}_{suffix}' if mix_quality else base_stem


def role_folder(base: Path, suffix: str, mix_quality: bool, role: str = 'part') -> Path:
    """按 mix_quality 为逻辑 base 生成 part/merged 目录；suffix 为画质标签，返回绝对目录路径，非法 role 报错。"""
    if role not in ('part', 'merged'):
        raise ToolError(f'目录用途无效：{role}')
    label = '' if mix_quality else '_' + suffix
    return base.with_name(base.name + label + ('_分P' if role == 'part' else '_合并'))
