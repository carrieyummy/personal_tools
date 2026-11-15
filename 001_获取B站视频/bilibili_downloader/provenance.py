"""计算可缓存的媒体内容指纹，并构造保留来源顺序的合并快照。"""

import hashlib
import json
import re
from pathlib import Path
from threading import Event
from typing import Any

from .errors import ToolError, check_cancel


def file_fingerprint(path: Path, cached: dict[str, Any] | None = None,
                     cancel_event: Event | None = None) -> dict[str, Any]:
    """为非空文件 path 计算 SHA-256；cached 按大小和纳秒修改时间复用，cancel_event 可中止，返回指纹字典。

    大小和修改时间均未变化的外部改写不在缓存检测范围内；读取期间文件变化时报错，不保存不完整摘要。
    """
    check_cancel(cancel_event)
    before = path.stat()
    if not path.is_file() or not before.st_size:
        raise ToolError(f'内容指纹要求非空普通文件：{path}')
    stamp = {'size_bytes': before.st_size, 'mtime_ns': before.st_mtime_ns}
    if (isinstance(cached, dict) and all(cached.get(key) == value for key, value in stamp.items())
            and isinstance(cached.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', cached['sha256'])):
        return {**stamp, 'sha256': cached['sha256']}
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            check_cancel(cancel_event)
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ToolError(f'计算内容指纹时源文件发生变化，请重试：{path}')
    return {**stamp, 'sha256': digest.hexdigest()}


def stream_version(cid: int, formats: list[dict[str, Any]]) -> str:
    """为正整数 cid 和稳定格式参数 formats 生成断点子目录名，返回不包含临时下载地址的版本标识。"""
    digest = hashlib.sha256(json.dumps(formats, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return f'cid{cid}_{digest}'


def source_snapshot(ordered: list[tuple[Path, dict[str, Any]]]) -> list[dict[str, Any]]:
    """为有序的 (路径, 源记录) 列表 ordered 更新指纹缓存，返回保留重复项且不包含路径的来源快照。"""
    result = []
    for path, item in ordered:
        fingerprint = file_fingerprint(path, item.get('content_fingerprint'))
        item['content_fingerprint'] = fingerprint
        result.append({'part': item['part'], 'source_cid': item.get('source_cid'),
                       'quality_id': item.get('quality_id'), 'sha256': fingerprint['sha256']})
        if item.get('episode_id') is not None:
            result[-1]['episode_id'] = item['episode_id']
    return result
