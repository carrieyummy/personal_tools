"""展开命令行链接和本地 YAML 下载清单，校验并合成逐项下载设置。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import api
from .config import Settings, expand_path
from .errors import ToolError


@dataclass(frozen=True)
class DownloadRequest:
    """描述一项输入：url 为规范链接，title 只用于日志，all_parts/merge_parts 控制范围和拼接。

    quality_rank 为清单任务档位并集中的正整数上限排名，None 表示普通链接交互选择；source 标识清单位置。
    quality_group 是本次运行共用档位排名确认的关联季 ss 标识集合，空集合表示独立选择；清单仍按原有规则选档。
    """

    url: str
    title: str
    all_parts: bool
    merge_parts: str
    quality_rank: int | None
    source: str
    quality_group: frozenset[str] = frozenset()


def is_manifest(value: str) -> bool:
    """判断 value 是否为本地 .yaml/.yml 路径；HTTP 链接始终视为视频输入，返回布尔值。"""
    return not value.lower().startswith(('http://', 'https://')) and Path(value).suffix.lower() in ('.yaml', '.yml')


def validate_options(values: dict[str, Any], label: str) -> None:
    """检查 values 中的清单覆盖项；label 为报错位置，非法类型或取值抛出 ToolError，无返回值。"""
    if 'all-parts' in values and type(values['all-parts']) is not bool:
        raise ToolError(f'{label}：all-parts 必须为 true 或 false')
    if 'quality' in values and (type(values['quality']) is not int or values['quality'] < 1):
        raise ToolError(f'{label}：quality 必须为正整数排名，1 表示最高可下载画质')
    if 'merge-parts' in values:
        value = values['merge-parts']
        if not isinstance(value, str) and (type(value) is not int or value not in (-1, 0)):
            raise ToolError(f'{label}：merge-parts 必须为 -1、0 或分组表达式字符串')


def load_manifest(path: Path, settings: Settings, all_parts: bool, merge_parts: str) -> list[DownloadRequest]:
    """读取 path 清单；settings/两个选项提供末级默认值，date 和 commit 为可选备注，返回已校验请求列表。"""
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8-sig'))
    except (OSError, yaml.YAMLError) as exc:
        raise ToolError(f'无法读取下载清单 {path}：{exc}') from exc
    if not isinstance(data, dict):
        raise ToolError(f'{path}：清单根节点必须为映射')
    unknown = data.keys() - {'date', 'commit', 'all-parts', 'merge-parts', 'quality', 'download_list'}
    if unknown:
        raise ToolError(f'{path}：未知全局配置 {sorted(unknown, key=str)}')
    validate_options(data, str(path))
    entries = data.get('download_list')
    if not isinstance(entries, list) or not entries:
        raise ToolError(f'{path}：download_list 必须为非空任务数组')
    requests = []
    for index, entry in enumerate(entries, 1):
        label = f'{path} 第 {index} 项'
        if not isinstance(entry, dict):
            raise ToolError(f'{label}：任务必须为映射')
        unknown = entry.keys() - {'title', 'url', 'all-parts', 'merge-parts', 'quality'}
        if unknown:
            raise ToolError(f'{label}：未知任务配置 {sorted(unknown, key=str)}')
        validate_options(entry, label)
        if not isinstance(entry.get('url'), str) or not entry['url'].strip():
            raise ToolError(f'{label}：url 必须为非空视频/番剧链接、BV/AV 或 ep/ss 号')
        if not isinstance(entry.get('title', ''), str):
            raise ToolError(f'{label}：title 必须为字符串，仅用于日志备注')
        quality = entry.get('quality', data.get('quality'))
        if quality is None:
            raise ToolError(f'{label}：缺少 quality，请在任务或清单全局设置画质排名')
        try:
            url = api.normalize_url(entry['url'])
        except (ToolError, ValueError) as exc:
            raise ToolError(f'{label}：{exc}') from exc
        requests.append(DownloadRequest(
            url, entry.get('title', ''), entry.get('all-parts', data.get('all-parts', all_parts or settings.all_parts)),
            str(entry.get('merge-parts', data.get('merge-parts', merge_parts))).strip() or '-1', quality, label,
        ))
    return requests


def expand_requests(values: list[str], settings: Settings, all_parts: bool, merge_parts: str) -> list[DownloadRequest]:
    """按 values 顺序展开链接及清单；settings/all_parts/merge_parts 为默认配置，返回请求列表。"""
    requests = []
    for value in values:
        value = value.strip()
        if is_manifest(value):
            path = expand_path(value, Path.cwd())
            requests.extend(load_manifest(path, settings, all_parts, merge_parts))
        else:
            requests.append(DownloadRequest(api.normalize_url(value), '', all_parts or settings.all_parts,
                                            merge_parts.strip() or '-1', None, value))
    return requests
