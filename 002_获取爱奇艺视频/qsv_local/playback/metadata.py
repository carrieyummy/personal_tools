"""解析官方链接及公开元数据，核对剧集和独立电影的来源及上映日期。"""

import base64
from datetime import date
import json
import re
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request
from .network import open_network
from .links import read_link_listing


def decode_play_id(profile: dict, path: str) -> str | None:
    """按 profile 中已核验的客户端规则解码 path 的 /v_*.html 标识；非该格式返回 None，越界或非法编号时报错。"""
    match = re.fullmatch(r"/v_([0-9a-zA-Z]+)\.html", path)
    if match is None:
        return None
    encoded = int(match[1], 36)
    # 客户端对应的 JavaScript Number 精度边界；超过它不能声称与客户端解码一致。
    if encoded > 2 ** 53 - 1:
        raise ValueError("播放页编号超过已核验的客户端整数精度范围")
    settings = profile["network"]["play_url_id"]
    value = encoded ^ settings["xor_mask"]
    if value < settings["legacy_threshold"]:
        value = settings["legacy_multiplier"] * (value + settings["legacy_offset"])
    if value <= 0:
        raise ValueError("播放页未解码出有效视频编号")
    return str(value)


def public_duration(value: str | int) -> int:
    """将官方资料的正整数秒或 MM:SS/HH:MM:SS 格式 value 转为正整数秒；格式无效时报错。"""
    if type(value) is int:
        total = value
    elif isinstance(value, str):
        parts = value.split(":")
        if (len(parts) not in (2, 3) or any(not item.isascii() or not item.isdecimal() for item in parts)
                or any(int(item) >= 60 for item in parts[1:])):
            raise ValueError("官方节目时长格式无效")
        total = 0
        for item in parts:
            total = total * 60 + int(item)
    else:
        raise ValueError("官方节目时长类型无效")
    if total <= 0:
        raise ValueError("官方节目时长必须大于零")
    return total


def linked_videos(profile: dict, indexes: list[int] | None = None, *, urls: list[str] | None = None) -> list[dict]:
    """解析 profile 的官方链接；indexes 指定下标，urls 为可选批次快照；返回包含页面链接的节目元数据。"""
    tasks = urls if urls is not None else read_link_listing(profile["download_list"])["download_list"]
    if indexes is not None and any(type(value) is not int or value < 0 or value >= len(tasks) for value in indexes):
        raise ValueError("链接下标超出当前清单范围")
    result = []
    for index, url in enumerate(tasks):
        if indexes is not None and index not in indexes:
            continue
        video, _ = resolve_video_link(profile, url, index)
        video["play_url"] = url
        result.append(video)
    return result


def _share_number(values: list[str], field: str) -> str:
    """解码分享字段 field 的唯一 Base64 值 values；返回正整数字符串，无效时报错。"""
    if len(values) != 1:
        raise ValueError(f"官方分享页必须提供唯一的 {field}")
    try:
        value = base64.b64decode(values[0], validate=True).decode("ascii")
    except ValueError:
        raise ValueError(f"官方分享页的 {field} 编码无效") from None
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError(f"官方分享页的 {field} 编号无效")
    return value


def resolve_video_link(profile: dict, url: str, index: int) -> tuple[dict, dict]:
    """解析 profile 下的官方 url，index 为清单下标；返回节目元数据和供研究记录使用的公开编号证据。"""
    network = profile["network"]
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in network["allowed_hosts"]:
        raise ValueError("只接受播放配置列出的爱奇艺官方链接")
    request = Request(url, headers={"User-Agent": network["user_agent"]})
    with open_network(profile, request) as response:
        target = urlsplit(response.url)
    if target.scheme not in ("http", "https") or target.hostname not in network["allowed_hosts"]:
        raise ValueError("官方链接跳转到未经验证的站点")
    query = parse_qs(target.query)
    values = query.get("tvid") or query.get("tvId")
    if not values and query.get("shareId"):
        values = [_share_number(query["shareId"], "shareId")]
    if not values:
        decoded = decode_play_id(profile, target.path)
        if decoded is not None:
            values = [decoded]
    if not values or len(set(values)) != 1 or not values[0].isascii() or not values[0].isdecimal() or int(values[0]) <= 0:
        raise ValueError("链接必须是可解析的官方短链接、分享页、正片播放页或含 tvid 的链接")
    tvid = values[0]
    data = _public_program(profile, tvid)
    evidence = {"link_tvid": tvid, "link_content_type": data.get("contentType")}
    if target.path == "/playShare.html" and data.get("contentType") != 1 and query.get("positiveId"):
        positive = _share_number(query["positiveId"], "positiveId")
        if positive != tvid:
            related = _public_program(profile, positive)
            if _is_movie(related):
                feature = str(data.get("featureVideoId"))
                if feature != positive or _share_number(query.get("shareId", []), "shareId") != tvid:
                    raise ValueError("官方分享页的 positiveId 与节目资料关联不一致")
                evidence.update(positive_tvid=positive, feature_tvid=feature)
                data = related
    if _is_movie(data):
        evidence.update(publish_date=data.get("publishDate"), platform_issue_day=data.get("formatIssueTime"))
    return _video_metadata(profile, data, index), evidence


def _public_program(profile: dict, tvid: str) -> dict:
    """按 profile 请求正整数编号 tvid，校验公开接口响应及视频编号后返回原始节目资料。"""
    if not isinstance(tvid, str) or not tvid.isascii() or not tvid.isdecimal() or int(tvid) <= 0:
        raise ValueError("公开节目编号必须为正整数字符串")
    network = profile["network"]
    request = Request(network["video_info_url"].format(tvid=tvid), headers={"User-Agent": network["user_agent"]})
    with open_network(profile, request) as response:
        data = response.read(network["max_response_bytes"] + 1)
    if len(data) > network["max_response_bytes"]:
        raise ValueError("节目资料超出配置的响应上限")
    response = json.loads(data.decode("utf-8-sig"))
    metadata = response.get("data") if isinstance(response, dict) else None
    if not isinstance(metadata, dict) or response.get("code") != "A00000" or str(metadata.get("tvId")) != tvid:
        raise ValueError("节目资料与目标视频编号不一致")
    return metadata


def _is_movie(data: dict) -> bool:
    """用 data 中已验证的频道、内容类型、独立节目结构及状态判断是否为电影正片。"""
    return (
        data.get("effective") is True
        and data.get("solo") is True
        and all(type(data.get(key)) is int and data[key] == 1 for key in ("channelId", "contentType", "videoCount", "order"))
        and str(data.get("albumId")) == str(data.get("tvId"))
    )


def _movie_release_date(value: str | None) -> str:
    """将电影 publishDate 的 YYYYMMDD 字符串 value 转为 ISO 院线上映日，无效时报错。"""
    if isinstance(value, str) and re.fullmatch(r"[0-9]{8}", value):
        try:
            return date(int(value[:4]), int(value[4:6]), int(value[6:])).isoformat()
        except ValueError:
            pass
    raise ValueError("电影正片缺少有效的 publishDate 院线上映日期")


def _video_metadata(profile: dict, metadata: dict, index: int) -> dict:
    """把已核验的节目 metadata 转为下标 index 的任务资料，按 profile 获取剧集日期或校验电影上映日。"""
    album, order = str(metadata.get("albumId")), metadata.get("order")
    if not album.isascii() or not album.isdecimal() or int(album) <= 0 or type(order) is not int or order <= 0:
        raise ValueError("官方节目缺少有效的专辑编号或集序")
    for key in ("albumName", "name", "vid"):
        if not isinstance(metadata.get(key), str) or not metadata[key]:
            raise ValueError("官方节目缺少有效的名称或视频来源编号")
    duration = public_duration(metadata["duration"])
    if _is_movie(metadata):
        if public_duration(metadata.get("durationSec")) != duration:
            raise ValueError("电影正片的两种公开时长声明不一致")
        if type(metadata.get("payMark")) is not int or metadata["payMark"] < 0:
            raise ValueError("电影正片缺少有效的付费标记")
        release = {"is_movie": True, "release_date": _movie_release_date(metadata.get("publishDate"))}
    else:
        release = profile["_album_cache"].get(profile, album)
    return {"index": index, "tvid": str(metadata["tvId"]), "album_id": album, "title": metadata["albumName"], "episode": order,
            "vid": metadata["vid"], "ischarge": bool(metadata["payMark"]), "duration_seconds": duration,
            "episode_title": metadata["name"], **release}


def public_video(profile: dict, tvid: str, index: int) -> dict:
    """按 profile 查询 tvid 对应的节目；index 为原清单下标，返回含上映日期的任务资料。"""
    return _video_metadata(profile, _public_program(profile, tvid), index)


def require_same_metadata(profile: dict, video: dict) -> None:
    """按 profile 复核 video 的官方类型、命名、来源及时长；日期或其他资料变化时报错，无返回值。"""
    current = public_video(profile, video["tvid"], video["index"])
    keys = ("is_movie", "album_id", "title", "episode", "episode_title", "vid", "duration_seconds", "release_date", "album_publish_time")
    if any(current.get(key) != video.get(key) for key in keys):
        raise RuntimeError("处理期间官方节目命名或来源元数据发生变化，停止本项处理")

