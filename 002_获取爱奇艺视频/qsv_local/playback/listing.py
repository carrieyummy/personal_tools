"""读取实际 YAML 下载任务并展开官方可用正片目录，不调用客户端界面或离线下载列表。"""

import json
from urllib.request import Request

import yaml

from .metadata import public_duration
from .links import read_link_listing
from .network import open_network


def task_options(profile: dict) -> list[dict]:
    """读取 profile 引用的清单及主配置；返回每个链接的全局画质与全剧设置，拒绝非法值。"""
    listing = read_link_listing(profile["download_list"])
    main = yaml.safe_load(profile["main_config"].read_text(encoding="utf-8-sig"))
    items = listing["download_list"]
    result = []
    for index, url in enumerate(items):
        quality = listing.get("quality")
        if "all-parts" in listing:
            all_parts = listing["all-parts"]
        else:
            all_parts = main["download"]["all_parts"]
        if type(quality) is not int or quality <= 0 or type(all_parts) is not bool:
            raise ValueError(f"清单第 {index} 项 quality 必须为正整数，all-parts 必须为布尔值")
        result.append({"index": index, "url": url, "quality": quality, "all_parts": all_parts})
    return result


def expand_episodes(profile: dict, link: dict, all_parts: bool, cancel=None) -> list[dict]:
    """按 profile 展开 link 的可用正片；all_parts 控制整剧，cancel 可取消分页；返回完整有序元数据。"""
    from ..runtime import check_cancel
    check_cancel(cancel)
    if not all_parts:
        return [link]
    network, result, ids, orders = profile["network"], [], set(), set()
    for page in range(1, network["max_episode_pages"] + 1):
        check_cancel(cancel)
        url = network["episode_list_url"].format(album_id=link["album_id"], page=page, page_size=network["episode_page_size"])
        with open_network(profile, Request(url, headers={"User-Agent": network["user_agent"]})) as response:
            raw = response.read(network["max_response_bytes"] + 1)
        if len(raw) > network["max_response_bytes"]:
            raise ValueError("专辑目录响应超过配置上限")
        value = json.loads(raw.decode("utf-8-sig"))
        data = value.get("data")
        if value.get("code") != "A00000" or not isinstance(data, dict) or str(data.get("albumId")) != link["album_id"]:
            raise ValueError("官方正片目录与目标专辑不一致")
        rows, more = data.get("epsodelist"), data.get("hasMore")
        if not isinstance(rows, list) or type(more) is not bool or more and not rows:
            raise ValueError("官方正片目录分页结构无效")
        for row in rows:
            if row.get("effective") is not True or row.get("contentType") != 1:
                continue
            order, tvid, vid = row.get("order"), str(row.get("tvId")), row.get("vid")
            if type(order) is not int or order <= 0 or not tvid.isascii() or not tvid.isdecimal() or int(tvid) <= 0 or not isinstance(vid, str) or not vid:
                raise ValueError("可用正片缺少有效的集序或视频编号")
            if tvid in ids or order in orders:
                raise ValueError("官方目录存在重复集序或视频编号")
            ids.add(tvid)
            orders.add(order)
            result.append(dict(link, episode=order, tvid=tvid, vid=vid, ischarge=bool(row["payMark"]),
                               duration_seconds=public_duration(row["duration"]), episode_title=row["name"], play_url=row["playUrl"]))
        if not more:
            if not result:
                raise ValueError("目标专辑没有可用的正片")
            return sorted(result, key=lambda item: item["episode"])
    raise ValueError("专辑目录超过配置的分页上限")
