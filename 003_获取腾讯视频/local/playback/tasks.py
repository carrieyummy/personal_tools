"""解析腾讯清单及官方分页选集；仅展开可识别集序的已上线正片，不混入速通版或花絮。"""

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

import yaml
from ..runtime import check_cancel, TaskCancelled

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


def requests(config: dict, *, require_single_video: bool = True) -> list[Request]:
    """读取 config 指定清单并返回保序请求；require_single_video 默认校验单集模式须有 vid，研究取样可关闭此项。"""
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
        if require_single_video and not whole and not vid:
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


def premiere_date(page: dict, settings: dict, series_id: str, series: str) -> str:
    """读取 page 中 settings 指定的整剧简介；series_id/series 核验来源，返回真实 YYYY-MM-DD 上映日期，缺失或冲突时报错。"""
    introductions = cards(page, settings["introduction_card"])
    if len(introductions) != 1 or introductions[0].get("params", {}).get("mod_title") != series:
        raise PlaybackError("腾讯整剧简介与剧名不一致")
    rows = children(introductions[0])
    matches = [row.get("params", {}) for row in rows if row.get("params", {}).get("report.cid") == series_id]
    if len(matches) != 1 or matches[0].get("title") != series:
        raise PlaybackError("腾讯整剧简介缺少唯一匹配的剧名或编号")
    value = matches[0].get("broadcast_time")
    try:
        if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise PlaybackError("腾讯整剧首次上映日期缺失或格式无效") from None
    return value


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
    release_date = premiere_date(page, settings, cid, series)
    modules = cards(page, settings["episode_card"])
    if len(modules) != 1:
        raise PlaybackError("需要唯一的正片选集模块")
    initial = modules[0]
    pending = [(initial, None)]
    contexts, page_sets, result = set(), set(), {}
    # 腾讯无分组页面会返回空字符串；分页仍由每页的 has_next 独立控制。
    raw_tabs = initial["params"]["tabs"]
    tabs = [] if raw_tabs == "" else json.loads(raw_tabs)
    if not isinstance(tabs, list):
        raise PlaybackError("选集分组格式无效")
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
                    "note": request.note, "source_type": "url", "release_date": release_date}
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


class Discovery:
    """逐清单项交接完整目录；在共享处理池查询，主调度线程补充日志归属。"""

    def __init__(self, client: NativeClient, values: list[Request], cancel=None):
        """保存 client、请求 values 及可选取消事件 cancel；初始化去重身份及发现进度。"""
        self.client, self.items, self.cancel = client, values, cancel
        self.position, self.discovered, self.seen = 0, 0, {}

    def progress(self, status: str | None = None) -> dict:
        """返回实际展开进度；status 可指定中断状态，未知剧集数量不估算。"""
        return {"status":status or ("completed" if self.position == len(self.items) else "running"),
                "processed_requests":self.position, "total_requests":len(self.items),
                "discovered_tasks":self.discovered}

    def group(self, request: Request, index: int | None = None) -> dict:
        """将 request 转为安全 URL 分组；index 为清单位置，省略时取当前展开位置。"""
        # 仅重建公开播放 URL，避免把分享链接中的临时查询参数写入报告。
        url = (f"https://v.qq.com/x/cover/{request.series_id}"
               + (f"/{request.video_id}" if request.video_id else "") + ".html"
               if request.series_id else f"https://v.qq.com/x/page/{request.video_id}.html")
        return {"id":str(self.position if index is None else index), "title":request.note, "url":url}

    def failure(self, request: Request, detail: str, index: int | None = None) -> dict:
        """将 request、脱敏 detail 和可选清单位置 index 转为条目；不虚构单集数量。"""
        return {"video_id":request.video_id or request.series_id, "title":request.note,
                "detail":detail, "groups":[self.group(request, index)]}

    def advance(self) -> dict:
        """查询下一清单项并完整校验后返回新增任务、失败及分组事件；取消和画质冲突终止批次。"""
        from .transport import safe_message
        check_cancel(self.cancel)
        request = self.items[self.position]
        group = {**self.group(request), "id":str(self.position)}
        tasks, failures, memberships = [], [], []
        try:
            episodes = catalog(self.client, request)
            check_cancel(self.cancel)
        except TaskCancelled:
            raise
        except (PlaybackError, KeyError, TypeError, ValueError) as error:
            failures.append({**self.failure(request, safe_message(error)), "groups":[group]})
            episodes = []
        # 整项先检查重复配置；发生冲突时不交接该项的一部分任务。
        for task in episodes:
            key = task["series_id"], task["video_id"]
            if key in self.seen and self.seen[key] != task["quality"]:
                raise PlaybackError("重复视频的 quality 冲突；已交接任务及成品保留")
        for task in episodes:
            key = task["series_id"], task["video_id"]
            membership = {**group, "title":request.note or task["series"]}
            if key in self.seen:
                memberships.append((key, membership))
                continue
            task = {**task, "groups":[membership]}
            self.seen[key] = task["quality"]
            tasks.append(task)
        self.position += 1
        self.discovered += len(tasks)
        return {"tasks":tasks, "failures":failures, "memberships":memberships, "progress":self.progress()}


def discover(client: NativeClient, values: list[Request]) -> tuple[list[dict], list[dict]]:
    """同步收集 client 的 values 目录；验收使用同一增量实现，返回全部任务及失败请求。"""
    source = Discovery(client, values)
    tasks, failed, seen = [], [], {}
    while source.position < len(values):
        result = source.advance()
        for task in result["tasks"]:
            tasks.append(task)
            seen[task["series_id"], task["video_id"]] = task
        for identity, group in result["memberships"]:
            seen[identity]["groups"].append(group)
        failed.extend(result["failures"])
    return tasks, failed
