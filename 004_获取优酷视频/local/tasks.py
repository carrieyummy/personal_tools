"""解析优酷链接清单和官方节目目录，保序展开正片并维护重复任务的全部归属。"""

from datetime import date
import json
import re
from urllib.parse import parse_qs, urlsplit

from .configuration import read_yaml
from .runtime import TaskCancelled, check_cancel
from .errors import safe_message
from .youku.client import PlaybackError


def video_id(url: str) -> str:
    """解析官方播放 url；支持 /video?vid= 和 /v_show/id_*.html，返回唯一合法视频 ID。"""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("v.youku.com", "m.youku.com"):
        raise PlaybackError("清单需要优酷官方播放链接")
    values = parse_qs(parsed.query).get("vid", [])
    match = re.fullmatch(r"/v_show/id_([A-Za-z0-9=]+)\.html/?", parsed.path)
    value = values[0] if len(values) == 1 and parsed.path.rstrip("/") == "/video" else match[1] if match else ""
    if not re.fullmatch(r"[A-Za-z0-9]+={0,2}", value):
        raise PlaybackError("播放链接缺少唯一合法 vid")
    return value


def page_url(vid: str) -> str:
    """将合法优酷视频编号 vid 转成官方播放页地址；无效编号抛错，返回页面 URL。"""
    if not isinstance(vid, str) or not re.fullmatch(r"[A-Za-z0-9]+={0,2}", vid):
        raise PlaybackError("官方目录视频编号无效")
    return "https://v.youku.com/video?vid=" + vid


def requests(config: dict) -> list[dict]:
    """读取 config 指定的 URL 列表，按清单全局/主配置读取设置；返回保序请求列表。"""
    root = read_yaml(config["default_list"])
    if not isinstance(root.get("download_list"), list) or not root["download_list"]:
        raise PlaybackError("download_list 必须为非空列表")
    result = []
    for number, item in enumerate(root["download_list"]):
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise PlaybackError(f"清单第 {number} 项必须是非空链接字符串，且首尾不能有空白")
        quality = root.get("quality", config["download"]["quality"])
        whole = root.get("all-parts", config["download"]["all_parts"])
        if type(quality) is not int or quality <= 0 or type(whole) is not bool:
            raise PlaybackError("清单 quality/all-parts 类型或取值无效")
        vid = video_id(item)
        result.append({"video_id":vid, "quality":quality, "all_parts":whole,
                       "url":page_url(vid), "list_index":number})
    return result


def release_date(client, vid: str, show_id: str, title: str) -> str:
    """请求 vid/show_id 的官方详情并核对 title；返回整剧真实首播 ISO 日期，缺失或矛盾时报错。"""
    options = client.profile["detail"]
    params = dict(options["params"], videoId=vid, showId=show_id, utdid=client.context["utid"])
    system = dict(options["system_info"], utdid=client.context["utid"])
    response = client.request(options, {"ms_codes":options["ms_codes"], "params":json.dumps(params), "system_info":json.dumps(system)})
    matches = []

    def visit(value) -> None:
        """遍历内存响应 value，只收集同时包含真实剧名和首播时间的对象；无返回值。"""
        if isinstance(value, dict):
            if "showReleaseTime" in value and value.get("showName") == title:
                matches.append(str(value["showReleaseTime"]).split(" ", 1)[0])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(response)
    if len(set(matches)) != 1:
        raise PlaybackError("官方详情缺少唯一匹配的整剧首播时间")
    result = matches[0]
    date.fromisoformat(result)
    return result


class Discovery:
    """逐项校验完整剧集目录；每次交接一项结果，不在工作线程修改已派发任务。"""

    def __init__(self, client, items: list[dict]):
        """保存 client 和保序请求 items；仅初始化目录缓存、去重集合和已完成请求数。"""
        self.client, self.items = client, items
        self.position, self.discovered = 0, 0
        self.shows, self.seen = {}, set()

    def progress(self, status: str | None = None) -> dict:
        """返回目录发现进度；status 可覆盖自动推导的运行或完成状态，不估算未知集数。"""
        return {"status": status or ("completed" if self.position == len(self.items) else "running"),
                "processed_requests": self.position, "total_requests": len(self.items),
                "discovered_tasks": self.discovered}

    @staticmethod
    def failure(request: dict, detail: str) -> dict:
        """将请求 request 与脱敏原因 detail 转为清单级失败条目；未知集数不伪造成单集。"""
        return {"video_id":request["video_id"], "title":request["video_id"],
                "detail":detail, "groups":[{"id":str(request["list_index"]),"url":request["url"],"title":"解析失败"}]}

    def advance(self) -> dict:
        """在共享处理池展开下一清单项；返回新增任务、分组补充及局部失败，取消直接传播。"""
        check_cancel(self.client.cancel)
        request = self.items[self.position]
        tasks, failures, memberships = [], [], []
        try:
            # 整项校验成功后才提交去重身份及任务，避免坏页或坏集导致半部剧提前下载。
            expanded = self.expand(request)
            check_cancel(self.client.cancel)
            for task in expanded:
                identity = (task["video_id"], task["quality"])
                if identity in self.seen:
                    memberships.append((identity, task["groups"][0]))
                else:
                    self.seen.add(identity)
                    tasks.append(task)
        except TaskCancelled:
            raise
        except Exception as error:
            failures.append(self.failure(request, safe_message(error)))
        self.position += 1
        self.discovered += len(tasks)
        return {"tasks":tasks, "failures":failures, "memberships":memberships, "progress":self.progress()}

    def expand(self, request: dict) -> list[dict]:
        """校验 request 对应整部剧的分页、首播日期及选中正片；返回按集序排列的独立任务。"""
        client, shows = self.client, self.shows
        body, _ = client.ups(request["video_id"])
        show = body.get("show", {})
        sid, title = show.get("encodeid"), show.get("title")
        if not isinstance(sid,str) or re.fullmatch(r"[A-Za-z0-9]+",sid) is None or not title:
            raise PlaybackError("无法确认官方节目编号及标题")
        if sid not in shows:
            premiered = release_date(client, request["video_id"], sid, title)
            episodes, ids, pages = [], set(), None
            for page in range(1, client.profile["catalog"]["max_pages"] + 1):
                check_cancel(client.cancel)
                model = client.catalog(request["video_id"], sid, page)
                info = model.get("showInfoDTO", {})
                if info.get("showId") != sid or info.get("showName") != title or model.get("curPage") != page:
                    raise PlaybackError("选集响应节目或页码不匹配")
                if pages is None:
                    pages = int(model["totalPages"])
                    total = int(model["total"])
                if pages != int(model["totalPages"]) or total != int(model["total"]):
                    raise PlaybackError("选集分页期间目录变化")
                values = model.get("videoItemList", [])
                if not values or len({x["vid"] for x in values}) != len(values) or any(x["vid"] in ids for x in values):
                    raise PlaybackError("选集返回空页或重复视频")
                ids.update(x["vid"] for x in values)
                episodes.extend(values)
                if page == pages:
                    break
            else:
                raise PlaybackError("选集页数超过配置上限")
            if len(ids) != total:
                raise PlaybackError("选集数量与官方总量不一致")
            shows[sid] = (premiered, episodes)
        premiered, episodes = shows[sid]
        selected = [x for x in episodes if x.get("videoType") == "正片" and
                    (request["all_parts"] or x["vid"] == request["video_id"])]
        if not selected:
            raise PlaybackError("清单链接未匹配已上线正片")
        tasks = []
        for item in sorted(selected, key=lambda x:int(x["stage"])):
            check_cancel(client.cancel)
            stage = int(item["stage"])
            if stage <= 0:
                raise PlaybackError("正片集序无效")
            group = {"id":str(request["list_index"]), "url":request["url"], "title":title}
            task = {"video_id":item["vid"], "series_id":sid, "series":title, "title":item["title"],
                    "index":stage, "release_date":premiered, "duration":float(item["seconds"]),
                    "quality":request["quality"], "groups":[group], "page_url":page_url(item["vid"])}
            if task["duration"] <= 0:
                raise PlaybackError("官方正片目录缺少有效时长")
            tasks.append(task)
        return tasks


def discover(client, items: list[dict]) -> tuple[list[dict], list[dict]]:
    """同步收集 client 的 items 完整目录；供限量验收使用，返回去重任务和局部失败。"""
    source = Discovery(client, items)
    tasks, failures, seen = [], [], {}
    while source.position < len(items):
        result = source.advance()
        for task in result["tasks"]:
            tasks.append(task)
            seen[(task["video_id"], task["quality"])] = task
        for identity, group in result["memberships"]:
            seen[identity]["groups"].append(group)
        failures.extend(result["failures"])
    return tasks, failures
