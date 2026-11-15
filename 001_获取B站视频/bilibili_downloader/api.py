"""规范化 B 站链接、调用认证接口并解析普通投稿及番剧单季目标。"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from .config import Settings
from .errors import ToolError
from .models import VideoTarget


API_ROOT = "https://api.bilibili.com"
HOME_URL = "https://www.bilibili.com/"
VIDEO_PATH = re.compile(r"^/video/(?P<id>BV[0-9A-Za-z]{10}|av[0-9]+)(?:/)?$", re.I)
BANGUMI_PATH = re.compile(r"^/bangumi/play/(?P<id>(?:ep|ss)[1-9][0-9]*)(?:/)?$", re.I)


def normalize_url(raw: str) -> str:
    """规范化 raw 中的 BV/AV、ep/ss 号和分享链接；普通投稿保留合法 p，番剧移除分享参数，返回 HTTPS 地址。"""
    raw = raw.strip()
    if re.fullmatch(r"BV[0-9A-Za-z]{10}|av[0-9]+", raw, re.I):
        raw = f"{HOME_URL}video/{raw}"
    elif re.fullmatch(r'(?:ep|ss)[1-9][0-9]*', raw, re.I):
        raw = f'{HOME_URL}bangumi/play/{raw}'
    else:
        match = re.search(r"https?://[^\s<>\"，。；！）)]+", raw)
        if match:
            raw = match.group(0)
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.port:
        raise ToolError("请输入 B 站视频或番剧链接、b23.tv 短链接或 BV/AV、ep/ss 号")
    if parsed.hostname == "b23.tv" and parsed.path.strip("/"):
        return f"https://b23.tv{parsed.path}"
    if parsed.hostname not in ("bilibili.com", "www.bilibili.com", "m.bilibili.com"):
        raise ToolError("仅支持 bilibili.com 视频、番剧和 b23.tv 短链接")
    bangumi = BANGUMI_PATH.fullmatch(parsed.path)
    if bangumi:
        return f"{HOME_URL}bangumi/play/{bangumi['id'].lower()}"
    match = VIDEO_PATH.fullmatch(parsed.path)
    if not match:
        raise ToolError("支持普通 BV/AV 视频及番剧 ep/ss 链接，不支持直播和课程")
    video_id = match["id"]
    video_id = ("BV" + video_id[2:]) if video_id[:2].lower() == "bv" else video_id.lower()
    parts = parse_qs(parsed.query, keep_blank_values=True).get("p")
    query = ""
    if parts:
        if len(parts) != 1 or not parts[0].isdigit() or int(parts[0]) < 1:
            raise ToolError("链接的 p 参数必须为一个正整数")
        query = "?" + urlencode({"p": int(parts[0])})
    return f"{HOME_URL}video/{video_id}{query}"


def api_json(context: Any, path: str, settings: Settings, params: dict[str, Any] | None = None,
             *, data_key: str = 'data', referer: str = HOME_URL) -> dict[str, Any]:
    """通过登录 context 请求 API path，返回成功响应；settings/params 控制请求，data_key/referer 由接口协议明确指定。"""
    response = context.request.get(
        API_ROOT + path, params=params, headers={"Referer": referer}, timeout=settings.request_timeout * 1000,
    )
    try:
        if not response.ok:
            raise ToolError(f"B 站接口 HTTP {response.status}：{path}")
        result = response.json()
        if result.get("code") != 0:
            raise ToolError(f"B 站接口 {path} 返回 {result.get('code')}：{result.get('message', '请求失败')}")
        if not isinstance(result.get(data_key), (dict, list)):
            raise ToolError(f"B 站接口数据格式异常：{path}")
        return result
    finally:
        response.dispose()


def resolve_url(context: Any, raw: str, settings: Settings) -> str:
    """通过 context/settings 规范化 raw 并解析 b23 短链，返回投稿或番剧的最终地址。"""
    url = normalize_url(raw)
    if urlsplit(url).hostname == "b23.tv":
        response = context.request.get(url, timeout=settings.request_timeout * 1000, max_redirects=5)
        try:
            url = normalize_url(response.url)
        finally:
            response.dispose()
        if urlsplit(url).hostname == "b23.tv":
            raise ToolError("b23.tv 短链接未能解析到视频或番剧")
    return url


def resolve_targets(context: Any, raw: str, settings: Settings, all_parts: bool) -> list[VideoTarget]:
    """解析 raw 的投稿或单季目标；context/settings 控制认证请求，all_parts 选择全部分P或本季正片，返回列表。"""
    url = resolve_url(context, raw, settings)
    parsed = urlsplit(url)
    if BANGUMI_PATH.fullmatch(parsed.path):
        from . import bangumi

        return bangumi.resolve_targets(context, url, settings, all_parts)
    video_id = VIDEO_PATH.fullmatch(parsed.path)["id"]
    params = {"bvid": video_id} if video_id.startswith("BV") else {"aid": video_id[2:]}
    pages = api_json(context, "/x/player/pagelist", settings, params)["data"]
    if not isinstance(pages, list) or not pages:
        raise ToolError("视频没有可下载的分 P，可能已删除或无访问权限")
    requested = int(parse_qs(parsed.query).get("p", ["1"])[0])
    selected = pages if all_parts else [page for page in pages if page.get("page") == requested]
    if not selected:
        raise ToolError(f"视频不存在第 {requested} P（共 {len(pages)} P）")
    return [VideoTarget(f"{HOME_URL}video/{video_id}?p={page['page']}", int(page["cid"]), int(page["page"]), len(pages)) for page in selected]


def target_metadata(context: Any, target: VideoTarget, settings: Settings) -> dict[str, Any]:
    """取得 target 所属来源的信息；番剧复用解析结果，投稿通过 context/settings 查询真实 BV 及全部分P，返回字典。"""
    if target.metadata is not None:
        return target.metadata
    video_id = VIDEO_PATH.fullmatch(urlsplit(target.url).path)['id']
    params = {'bvid': video_id} if video_id.startswith('BV') else {'aid': video_id[2:]}
    return api_json(context, '/x/web-interface/view', settings, params)['data']
