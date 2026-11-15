"""统一普通投稿和番剧的来源身份，保留旧索引中真实 BV 号的读取能力。"""

from __future__ import annotations

import re
from typing import Any

from .errors import ToolError


SOURCE_ID = re.compile(r'(?:BV[0-9A-Za-z]{10}|(?:ss|ep)[1-9][0-9]*)')


def source_id(data: dict[str, Any]) -> str:
    """读取 data 的统一来源标识；旧投稿允许只有真实 bvid，返回有效 BV/ss/ep 标识，否则报错。"""
    value = data.get('source_id') if 'source_id' in data else data.get('bvid')
    if not isinstance(value, str) or not SOURCE_ID.fullmatch(value):
        raise ToolError('来源标识无效，必须为 BV、ss 或 ep 编号')
    return value


def fields(identifier: str) -> dict[str, Any]:
    """为有效 identifier 返回来源类型、标识和真实 BV 字段；番剧整体没有单一 BV，使用 None。"""
    value = source_id({'source_id': identifier})
    video = value.startswith('BV')
    return {'source_type': 'video' if video else 'bangumi', 'source_id': value,
            'bvid': value if video else None}


def validate(data: dict[str, Any], *, legacy: bool = False) -> None:
    """校验 data 身份；legacy 为 True 时仅将旧投稿身份补齐，不更改原媒体、地址和索引版本，无返回值。"""
    expected = fields(source_id(data))
    if legacy:
        if not expected['bvid']:
            raise ToolError('旧索引必须包含真实 BV 号')
        data.update(expected)
    elif any(data.get(key) != value for key, value in expected.items()):
        raise ToolError('来源类型、来源标识与真实 BV 号不一致')


def source_url(identifier: str) -> str:
    """根据有效 identifier 返回规范的普通投稿或番剧来源地址。"""
    value = source_id({'source_id': identifier})
    path = 'video/' if value.startswith('BV') else 'bangumi/play/'
    return 'https://www.bilibili.com/' + path + value
