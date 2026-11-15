"""定义下载格式版本和拼接方案，共用编码兼容性及稳定版本标识。"""

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from .errors import ToolError
from .models import Quality


def compatibility(formats: list[dict[str, Any]]) -> tuple[tuple[Any, ...], ...]:
    """提取 formats 有序流的编码和尺寸；返回兼容性签名，缺少格式记录时报错。"""
    if not isinstance(formats, list) or not formats or any(not isinstance(fmt, dict) for fmt in formats):
        raise ToolError('下载记录缺少编码信息，无法安全拼接')
    return tuple(tuple(fmt.get(key) for key in ('vcodec', 'acodec', 'width', 'height')) for fmt in formats)


def variant_id(formats: list[dict[str, Any]]) -> str:
    """对 formats 的有序编码、尺寸及帧率求摘要；不包含地址或格式编号，返回稳定的完整 SHA-256。"""
    compatibility(formats)
    values = [[fmt.get(key) for key in ('vcodec', 'acodec', 'width', 'height', 'fps')] for fmt in formats]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def source_id(source: str, quality: Quality, variant: str = '') -> str:
    """为原始 source 标识追加 quality 和可选完整格式 variant；返回源条目标识。"""
    from .models import quality_entry_id
    return quality_entry_id(source, quality) + (f':fmt{variant}' if variant else '')


def label(formats: list[dict[str, Any]]) -> str:
    """把 formats 的编码和视频尺寸转为用户可读的版本说明；返回字符串。"""
    return ' + '.join('/'.join(str(fmt.get(key)) for key in ('vcodec', 'acodec', 'width', 'height')
                              if fmt.get(key) not in (None, 'none')) for fmt in formats)


@dataclass(frozen=True)
class StreamChoice:
    """一个实际选流版本：quality 为档位，formats 为不含临时地址的选流参数；rank 为格式偏好顺序。"""

    quality: Quality
    formats: tuple[dict[str, Any], ...]
    rank: tuple[int, ...] = field(default=(), compare=False)

    @property
    def variant(self) -> str:
        """返回此选流的稳定格式版本标识；无参数。"""
        return variant_id(list(self.formats))


@dataclass
class MergePlan:
    """一组拼接计划：expression/parts 保留顺序与重复，choices 按P指定版本，error 为不可拼接原因。"""

    expression: str
    parts: list[int] | None
    choices: dict[int, StreamChoice] = field(default_factory=dict)
    error: str = ''


def find_sources(record: Any, choices: dict[int, StreamChoice]) -> dict[int, str]:
    """在 record 中按 choices 精确定位档位及格式版本；返回 P 到条目标识，缺失或歧义时报错。"""
    result = {}
    for part, choice in choices.items():
        matches = [item for item in record.data['files'] if item.get('kind') == 'part' and item.get('part') == part
                   and item.get('quality_id') == choice.quality.code and item.get('formats')
                   and variant_id(item['formats']) == choice.variant]
        if len(matches) != 1:
            raise ToolError(f'P{part} {choice.quality.name} 的指定编码版本缺失或不唯一')
        result[part] = matches[0]['id']
    return result
