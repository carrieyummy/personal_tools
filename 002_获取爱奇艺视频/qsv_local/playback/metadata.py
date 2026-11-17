"""解析官方链接及公开视频元数据，保持目标、专辑和集序一致。"""

import base64
import json
import re
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request
import yaml

from .network import open_network


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


def linked_videos(profile: dict, indexes: list[int] | None = None) -> list[dict]:
    """解析 profile 的官方链接及公开资料；indexes 可指定原清单下标，None 为全部；返回节目元数据，不使用票据。"""
    listing = yaml.safe_load(profile["download_list"].read_text(encoding="utf-8-sig"))
    tasks = listing.get("download_list") if isinstance(listing, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("download_list 必须为非空列表")
    if indexes is not None and any(type(value) is not int or value < 0 or value >= len(tasks) for value in indexes):
        raise ValueError("链接下标超出当前清单范围")
    network, result = profile["network"], []
    for index, item in enumerate(tasks):
        if indexes is not None and index not in indexes:
            continue
        url = item["url"]
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in network["allowed_hosts"]:
            raise ValueError("只接受研究配置中列出的爱奇艺官方链接")
        request = Request(url, headers={"User-Agent": network["user_agent"]})
        with open_network(profile, request) as response:
            target = urlsplit(response.url)
        if target.hostname not in network["allowed_hosts"]:
            raise ValueError("官方链接跳转到未经验证的站点")
        query = parse_qs(target.query)
        values = query.get("tvid") or query.get("tvId")
        if not values and query.get("shareId"):
            values = [base64.b64decode(query["shareId"][0], validate=True).decode("ascii")]
        if not values:
            decoded = decode_play_id(profile, target.path)
            if decoded is not None:
                values = [decoded]
        if not values or len(set(values)) != 1 or not values[0].isascii() or not values[0].isdecimal() or int(values[0]) <= 0:
            raise ValueError("链接必须是可解析的官方短链接、分享页、正片播放页或含 tvid 的链接")
        tvid = values[0]
        result.append(public_video(profile, tvid, index))
    return result


def public_video(profile: dict, tvid: str, index: int) -> dict:
    """按 profile 查询正整数编号 tvid；index 为原清单位置；返回视频、命名与专辑级上映日期，日期在本次 profile 内按专辑固定。"""
    if not isinstance(tvid, str) or not tvid.isascii() or not tvid.isdecimal() or int(tvid) <= 0:
        raise ValueError("公开节目编号必须为正整数字符串")
    network = profile["network"]
    request = Request(network["video_info_url"].format(tvid=tvid), headers={"User-Agent": network["user_agent"]})
    with open_network(profile, request) as response:
        data = response.read(network["max_response_bytes"] + 1)
    if len(data) > network["max_response_bytes"]:
        raise ValueError("节目资料超出配置的响应上限")
    response = json.loads(data.decode("utf-8-sig"))
    metadata = response.get("data")
    if response.get("code") != "A00000" or not isinstance(metadata, dict) or str(metadata.get("tvId")) != tvid:
        raise ValueError("节目资料与目标视频编号不一致")
    album, order = str(metadata.get("albumId")), metadata.get("order")
    if not album.isascii() or not album.isdecimal() or int(album) <= 0 or type(order) is not int or order <= 0:
        raise ValueError("官方节目缺少有效的专辑编号或集序")
    for key in ("albumName", "name", "vid"):
        if not isinstance(metadata.get(key), str) or not metadata[key]:
            raise ValueError("官方节目缺少有效的名称或视频来源编号")
    release = profile["_album_cache"].get(profile, album)
    return {"index": index, "tvid": tvid, "album_id": album, "title": metadata["albumName"], "episode": order,
            "vid": metadata["vid"], "ischarge": bool(metadata["payMark"]), "duration_seconds": public_duration(metadata["duration"]),
            "episode_title": metadata["name"], **release}


def require_same_metadata(profile: dict, video: dict) -> None:
    """核对 profile 中 video 的官方命名及来源，日期与批次专辑快照保持一致；发生变化时报错，无返回值，不迁移既有成品。"""
    current = public_video(profile, video["tvid"], video["index"])
    if any(current[key] != video[key] for key in ("album_id", "title", "episode", "episode_title", "vid", "duration_seconds", "release_date", "album_publish_time")):
        raise RuntimeError("处理期间官方节目命名或来源元数据发生变化，停止本项处理")

