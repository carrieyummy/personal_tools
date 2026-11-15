"""解析番剧关联季和剧集，用 PGC 原站接口取得完整播放流并适配共享下载流程。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from . import api, identity
from .config import Settings
from .errors import ToolError
from .models import VideoTarget


def positive_id(value: Any, label: str) -> int:
    """校验原站 value 是否为正整数；label 描述字段，返回整数，异常响应明确报错。"""
    if type(value) is not int or value < 1:
        raise ToolError(f'番剧接口的 {label} 无效')
    return value


def episode_page(episode: dict[str, Any], part: int) -> dict[str, Any]:
    """将原站 episode 转为编号 part 的规范分P信息，保留真实剧集身份并返回字典。"""
    if not isinstance(episode, dict):
        raise ToolError('番剧剧集列表格式异常')
    ep = positive_id(episode.get('id'), 'EP')
    cid = positive_id(episode.get('cid'), 'CID')
    aid = positive_id(episode.get('aid'), 'AV')
    bvid = episode.get('bvid')
    if bvid is not None and (not isinstance(bvid, str) or not api.VIDEO_PATH.fullmatch('/video/' + bvid)
                             or not bvid.startswith('BV')):
        raise ToolError('番剧接口的真实 BV 号无效')
    title = ' '.join(str(episode.get(key) or '').strip() for key in ('title', 'long_title')).strip()
    return {'page': part, 'cid': cid, 'episode_id': ep, 'aid': aid, 'bvid': bvid,
            'part': title or f'第 {part} 集', 'source_url': f'{api.HOME_URL}bangumi/play/ep{ep}'}


def load_season(context: Any, url: str, settings: Settings) -> dict[str, Any]:
    """通过 context/settings 查询规范番剧 url 的季信息，校验季编号后返回原站字典。"""
    identifier = api.BANGUMI_PATH.fullmatch(urlsplit(url).path)['id']
    params = {'ep_id' if identifier.startswith('ep') else 'season_id': int(identifier[2:])}
    season = api.api_json(context, '/pgc/view/web/season', settings, params, data_key='result')['result']
    if not isinstance(season, dict):
        raise ToolError('番剧季信息格式异常')
    season_id = positive_id(season.get('season_id'), '季编号')
    if identifier.startswith('ss') and season_id != int(identifier[2:]):
        raise ToolError('番剧接口返回的季编号与请求不一致')
    return season


def related_season_urls(context: Any, url: str, settings: Settings) -> list[str]:
    """按 context/settings 查询番剧 url 的关联季，按原站顺序去重并补入当前季，返回规范 ss 地址列表。"""
    season = load_season(context, url, settings)
    seasons = season.get('seasons')
    if not isinstance(seasons, list):
        raise ToolError('番剧缺少有效的关联季列表，无法下载所有季')
    identifiers = []
    for item in seasons:
        if not isinstance(item, dict):
            raise ToolError('番剧关联季列表格式异常')
        identifiers.append(positive_id(item.get('season_id'), '关联季编号'))
    identifiers.append(season['season_id'])
    return [identity.source_url(f'ss{identifier}') for identifier in dict.fromkeys(identifiers)]


def resolve_targets(context: Any, url: str, settings: Settings, all_parts: bool) -> list[VideoTarget]:
    """按 context/settings 解析规范番剧 url；all_parts 取同季正片，否则取指定集或季首集，返回共享下载目标。"""
    identifier = api.BANGUMI_PATH.fullmatch(urlsplit(url).path)['id']
    season = load_season(context, url, settings)
    season_id = season['season_id']
    title = season.get('title')
    if not isinstance(title, str) or not title.strip():
        raise ToolError('番剧标题为空，无法创建目录')
    episodes = season.get('episodes')
    if not isinstance(episodes, list):
        raise ToolError('番剧缺少正片列表')
    pages = [episode_page(episode, part) for part, episode in enumerate(episodes, 1)]
    requested = int(identifier[2:]) if identifier.startswith('ep') else None
    selected = pages if all_parts else ([page for page in pages if page['episode_id'] == requested]
                                        if requested else pages[:1])
    source_id = f'ss{season_id}'
    if requested is not None and not any(page['episode_id'] == requested for page in pages):
        extra = [episode for section in season.get('section', []) for episode in section.get('episodes', [])
                 if episode.get('id') == requested]
        if len(extra) != 1:
            raise ToolError(f'番剧季列表中找不到 ep{requested}')
        if not all_parts:
            source_id = f'ep{requested}'
            pages = selected = [episode_page(extra[0], 1)]
            title += '_' + pages[0]['part']
    if not pages or not selected:
        raise ToolError('番剧没有可下载的正片，可能未上线、已下架或无访问权限')
    if len({page['episode_id'] for page in pages}) != len(pages):
        raise ToolError('番剧列表包含重复 EP，无法确定剧集顺序')
    metadata = {**identity.fields(source_id), 'title': title, 'source_url': identity.source_url(source_id),
                'season_id': season_id, 'pages': pages, 'videos': len(pages), 'owner': None}
    return [VideoTarget(page['source_url'], page['cid'], page['page'], len(pages), source_id,
                        page['episode_id'], season_id, page['bvid'], page['aid'], page['part'], metadata)
            for page in selected]


def extract_info(context: Any, downloader: Any, target: VideoTarget, settings: Settings) -> dict[str, Any]:
    """经 context/settings 查询 target 的完整 PGC 流，用 downloader 的格式解析器转换并返回单集信息；拒绝试看和 DRM。"""
    result = api.api_json(context, '/pgc/player/web/v2/playurl', settings,
                          {'ep_id': target.episode_id, 'cid': target.cid, 'fnval': 12240},
                          data_key='result', referer=target.url)['result']
    if not isinstance(result, dict) or not isinstance(result.get('video_info'), dict):
        raise ToolError('番剧播放接口缺少视频信息，可能无观看权限')
    video = result['video_info']
    if video.get('is_preview') or result.get('play_video_type') == 'preview' or result.get('play_check', {}).get('play_detail') == 'PLAY_PREVIEW':
        raise ToolError('当前账号只能试看，未取得完整剧集；请检查会员或购买权限')
    if video.get('is_drm'):
        raise ToolError('该剧集仅提供 DRM 保护流，无法保存为普通媒体文件')
    if video.get('code', 0) != 0:
        raise ToolError(f"番剧播放失败：{video.get('message', video['code'])}")
    formats = downloader.get_info_extractor('BiliBiliBangumi').extract_formats(video)
    if not formats:
        raise ToolError('番剧没有可下载的完整播放流，请检查登录、地区或观看权限')
    return {'id': f'{target.source_id}_p{target.part}', 'title': target.title,
            'webpage_url': target.url, 'episode_id': target.episode_id, 'season_id': target.season_id,
            'source_id': target.source_id, 'bvid': target.bvid, 'aid': target.aid, 'formats': formats,
            'duration': video.get('timelength', 0) / 1000,
            'http_headers': {'Referer': target.url}}
