"""解析腾讯清单及官方分页选集；仅展开可识别集序的已上线正片，不混入速通版或花絮。"""

from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

import yaml

from .client import NativeClient, video_info
from .errors import PlaybackError


@dataclass(frozen=True)
class Request:
    """清单请求；series_id/video_id 为官方编号，quality 为逐集排名，all_parts 控制整剧，note 为备注。"""

    series_id: str
    video_id: str
    quality: int
    all_parts: bool
    note: str


def normalize_url(value: str) -> tuple[str, str]:
    """解析官方 HTTP/S value；支持移动分享和桌面剧集/单集地址；返回 cid/vid，缺省编号为空串。"""
    parsed = urlsplit(value.strip())
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("v.qq.com", "m.v.qq.com", "www.v.qq.com"):
        raise PlaybackError("url 必须是腾讯视频官方 HTTP/S 播放链接")
    query = parse_qs(parsed.query)
    if parsed.path.rstrip("/") == "/x/m/play":
        cid, vid = query.get("cid", [""])[0], query.get("vid", [""])[0]
    else:
        cover = re.fullmatch(r"/x/cover/([A-Za-z0-9]+)(?:/([A-Za-z0-9]+))?\.html/?", parsed.path)
        single = re.fullmatch(r"/x/page/([A-Za-z0-9]+)\.html/?", parsed.path)
        if cover:
            cid, vid = cover[1], cover[2] or ""
        elif single:
            cid, vid = "", single[1]
        else:
            raise PlaybackError("不支持的腾讯播放链接格式")
    if not cid and not vid or any(value and re.fullmatch(r"[A-Za-z0-9]+", value) is None for value in (cid, vid)):
        raise PlaybackError("链接中的 cid/vid 缺失或非法")
    return cid, vid


def requests(config: dict) -> list[Request]:
    """读取 config 指定的清单；按任务、清单、主配置继承选项，返回保序请求列表。"""
    root = yaml.safe_load(config["default_list"].read_text(encoding="utf-8-sig"))
    if not isinstance(root, dict) or not isinstance(root.get("download_list"), list) or not root["download_list"]:
        raise PlaybackError("download_list 必须是非空列表")
    result = []
    for item in root["download_list"]:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise PlaybackError("每个清单项必须包含字符串 url")
        quality = item.get("quality", root.get("quality", config["download"]["quality"]))
        whole = item.get("all-parts", root.get("all-parts", config["download"]["all_parts"]))
        if type(quality) is not int or quality <= 0 or type(whole) is not bool:
            raise PlaybackError("quality 必须是正整数，all-parts 必须是布尔值")
        if "merge-parts" in root or "merge-parts" in item:
            raise PlaybackError("腾讯工具不支持 merge-parts")
        cid, vid = normalize_url(item["url"])
        if not whole and not vid:
            raise PlaybackError("仅下载单集时，链接须包含 vid")
        result.append(Request(cid, vid, quality, whole, str(item.get("title", ""))))
    return result


def cards(page: dict, kind: str) -> list[dict]:
    """从 page 的 CardList 提取 kind 类型模块；返回匹配列表，缺少列表时报结构错误。"""
    values = page.get("CardList")
    if not isinstance(values, list):
        raise PlaybackError("选集响应缺少 CardList")
    return [value for value in values if value.get("type") == kind]


def children(card: dict) -> list[dict]:
    """展开原生 card 的 children_list；返回子卡片，不依赖字典键的字符串排序。"""
    groups = card.get("children_list")
    if not isinstance(groups, dict):
        raise PlaybackError("选集卡片缺少 children_list")
    return [item for group in groups.values() for item in group["cards"]]


def catalog(client: NativeClient, request: Request) -> list[dict]:
    """展开 client 中 request 的全部选集分组和分页；返回按集序排列的正片任务，重复页或矛盾记录报错。"""
    cid, vid = request.series_id, request.video_id
    if not cid:
        native = video_info(client.source("", vid))
        cid = str(native.get("cid", ""))
        if re.fullmatch(r"[A-Za-z0-9]+", cid) is None:
            raise PlaybackError("无法唯一确定整剧编号，请提供含 cid 的链接")
    settings = client.profile["catalog"]
    page = client.catalog_page(cid, vid)
    introductions = cards(page, settings["introduction_card"])
    if not introductions or not introductions[0]["params"].get("mod_title"):
        raise PlaybackError("选集接口缺少源站剧名")
    series = introductions[0]["params"]["mod_title"]
    modules = cards(page, settings["episode_card"])
    if len(modules) != 1:
        raise PlaybackError("需要唯一的正片选集模块")
    initial = modules[0]
    pending = [(initial, None)]
    contexts, page_sets, result = set(), set(), {}
    tabs = json.loads(initial["params"]["tabs"])
    for tab in tabs:
        if tab.get("selected") is not True:
            pending.append((None, tab["page_context"]))
    count = 0
    while pending:
        module, context = pending.pop(0)
        count += 1
        if count > settings["max_pages"]:
            raise PlaybackError("选集页数超过专项配置限制")
        if context is not None:
            if not isinstance(context, str) or not context or context in contexts:
                raise PlaybackError("选集分页游标缺失或重复")
            contexts.add(context)
            matches = cards(client.catalog_page(cid, vid, context, initial["params"]["page_id"]), settings["episode_card"])
            if len(matches) != 1:
                raise PlaybackError("后续选集页缺少唯一正片模块")
            module = matches[0]
        rows = children(module)
        identity = tuple(sorted(str(row["params"].get("vid")) for row in rows))
        if not identity or identity in page_sets:
            raise PlaybackError("选集返回空页或重复页，停止以免漏集")
        page_sets.add(identity)
        for row in rows:
            params = row["params"]
            index = params.get("title", "")
            if re.fullmatch(r"[0-9]+", index) is None or params.get("is_trailer") != "0":
                continue
            if params.get("cid") != cid or re.fullmatch(r"[A-Za-z0-9]+", params.get("vid", "")) is None:
                raise PlaybackError("选集条目的 cid/vid 不一致")
            task = {"series_id": cid, "video_id": params["vid"], "series": series,
                    "title": params["play_title"], "index": int(index), "quality": request.quality,
                    "note": request.note, "source_type": "url"}
            previous = result.get(task["video_id"])
            if previous is not None and previous != task:
                raise PlaybackError("同一视频的选集元数据冲突")
            result[task["video_id"]] = task
        more = module["params"]["has_next"]
        if more not in ("true", "false"):
            raise PlaybackError("选集分页完成状态无法识别")
        if more == "true":
            pending.append((None, module["params"]["next_page_context"]))
    tasks = sorted(result.values(), key=lambda task: (task["index"], task["video_id"]))
    if not request.all_parts:
        tasks = [task for task in tasks if task["video_id"] == vid]
    if not tasks:
        raise PlaybackError("链接没有对应的已上线正片")
    return tasks


def discover(client: NativeClient, values: list[Request]) -> tuple[list[dict], list[dict]]:
    """按 values 顺序展开并去重；client 查询目录；返回任务及失败请求，重复参数冲突中止批次。"""
    tasks, failed, seen = [], [], {}
    from .errors import AuthenticationError
    for request in values:
        try:
            episodes = catalog(client, request)
        except AuthenticationError:
            raise
        except (PlaybackError, KeyError, TypeError, ValueError) as error:
            from .transport import safe_message
            failed.append({"video_id": request.video_id or request.series_id, "title": request.note,
                           "detail": safe_message(error)})
            continue
        for task in episodes:
            key = task["series_id"], task["video_id"]
            if key in seen:
                if seen[key] != task["quality"]:
                    raise PlaybackError("重复视频的 quality 冲突")
                continue
            seen[key] = task["quality"]
            tasks.append(task)
    return tasks, failed
