"""解析链接清单、逐页正片目录和接口画质档位，不按营销名称猜测真实格式。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .client import NativeClient, normalize_url
from .configuration import positive, read_yaml
from .errors import ToolError


@dataclass(frozen=True)
class Request:
    """用户任务；url 为官方链接，quality 为逐集排名，all_parts 控制整剧，note 为日志备注。"""

    url: str
    quality: int
    all_parts: bool
    note: str = ""


@dataclass(frozen=True)
class Episode:
    """源站正片；编号为数字串，index 为集序号，title/series 为源站标题，released 表示已上线。"""

    clip_id: str
    video_id: str
    index: int
    title: str
    series: str
    released: bool
    quality: int
    note: str = ""

    @property
    def key(self) -> str:
        """返回稳定任务编号；无参数；同一剧集视频只调度一次。"""
        return f"{self.clip_id}/{self.video_id}"


def requests(values: list[str], config: dict[str, Any], all_parts: bool | None) -> list[Request]:
    """解析 values 中的链接或 YAML 路径；config 提供默认值，all_parts 为 CLI 覆盖；返回保序任务。"""
    result = []
    for value in values:
        if value.lower().startswith(("https://", "http://")):
            root, items = {}, [{"url": value}]
        else:
            root = read_yaml(Path(value).expanduser().resolve())
            items = root.get("download_list")
            if not isinstance(items, list) or not items:
                raise ToolError("download_list 必须为非空任务列表")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                raise ToolError("每个任务必须包含字符串 url")
            quality = item.get("quality", root.get("quality", config["download"]["quality"]))
            whole = all_parts if all_parts is not None else item.get("all-parts", root.get("all-parts", config["download"]["all_parts"]))
            positive(quality, "quality", integer=True)
            if type(whole) is not bool:
                raise ToolError("all-parts 必须为布尔值")
            if "merge-parts" in root or "merge-parts" in item:
                raise ToolError("芒果工具不支持 merge-parts，请删除该字段")
            url, _, _ = normalize_url(item["url"])
            result.append(Request(url, quality, whole, str(item.get("title", ""))))
    return result


def catalog(client: NativeClient, request: Request) -> list[Episode]:
    """读取全部选集页；client 为原生接口，request 控制范围；返回正片，分页异常时报错防止漏集。"""
    try:
        return catalog_rows(client, request)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ToolError("选集接口结构变化或字段无效，请检查研究报告") from error


def catalog_rows(client: NativeClient, request: Request) -> list[Episode]:
    """解析目录字段；client 查询分页，request 控制范围；返回按集序排列的正片，结构错误由 catalog 统一转换。"""
    _, clip_id, linked_id = normalize_url(request.url)
    first = client.catalog_page(clip_id, 1)
    pages = int(first["pageTotal"])
    if pages < 1:
        raise ToolError("选集接口页数无效")
    rows, seen_pages = [], set()
    for page in range(1, pages + 1):
        data = first if page == 1 else client.catalog_page(clip_id, page)
        items = data.get("list")
        if not isinstance(items, list):
            raise ToolError("选集接口 list 结构变化")
        identity = tuple(str(item.get("videoId")) for item in items)
        if identity in seen_pages:
            raise ToolError("选集分页返回重复页，停止以免漏集")
        seen_pages.add(identity)
        rows.extend(items)
    if len({str(row.get("videoId")) for row in rows if int(row["type"]) == 1}) != int(first["total"]):
        raise ToolError("选集总数与完整分页结果不符")
    title = str(first["clipInfo"]["clipName"])
    selected = [row for row in rows if int(row["type"]) == 1 and (request.all_parts or str(row["videoId"]) == linked_id)]
    if not selected:
        raise ToolError("链接对应视频不在正片目录中")
    result = []
    for row in selected:
        if str(row["clipId"]) != clip_id or not str(row["videoId"]).isdigit():
            raise ToolError("选集条目编号不一致")
        result.append(Episode(clip_id, str(row["videoId"]), int(row["videoIndex"]), str(row.get("nname") or row["name"]), title,
                              int(row["status"]) == 1, request.quality, request.note))
    return sorted(result, key=lambda episode: (episode.index, episode.video_id))


def streams(data: dict[str, Any]) -> list[dict[str, Any]]:
    """提取接口由高到低的画质组；data 为授权响应；排除非会员明确付费档，返回每档首选源，不采用低规格备用源。"""
    values = data.get("videoSources")
    if not isinstance(values, list) or not values:
        raise ToolError("账号未获得可用视频源")
    result, seen = [], set()
    for value in values:
        if not isinstance(value, dict) or not value.get("definition") or not value.get("fileId") or not value.get("fileHash"):
            raise ToolError("视频源结构变化或缺少稳定身份")
        definition = str(value["definition"])
        if data.get("user", {}).get("isVip") == 0 and value.get("needPay") == 1:
            continue
        if definition not in seen:
            result.append(value)
            seen.add(definition)
    if not result:
        raise ToolError("当前账号未获得可用画质档位")
    return result


def describe(stream: dict[str, Any]) -> dict[str, Any]:
    """生成可落盘的脱敏源描述；stream 为单个源；返回编码、尺寸、HDR 等原始元数据，不含签名地址。"""
    fields = ("definition", "originDef", "standardName", "intention", "videoWidth", "videoHeight", "framerate", "videoFormat", "audioFormat",
              "audioTrack", "filebitrate", "fileSize", "fileFormat", "fileId", "fileHash", "ftime", "isHdr", "hdrType")
    return {field: stream[field] for field in fields if field in stream}


def choose(data: dict[str, Any], rank: int) -> dict[str, Any]:
    """选择逐集 rank 档（1 最高）；data 为授权响应；返回具体源，档位不存在时报错，不降级。"""
    values = streams(data)
    if rank > len(values):
        raise ToolError(f"本集仅有 {len(values)} 档画质，无法选择第 {rank} 档")
    return values[rank - 1]


def resolved(client: NativeClient, episode: Episode, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    """取得选定源及内嵌清单；client/episode 为请求上下文，expected 可固定刷新前源；返回含内存地址的源。"""
    selected = choose(client.source(episode.clip_id, episode.video_id), episode.quality) if expected is None else expected
    data = client.source(episode.clip_id, episode.video_id, str(selected["definition"]))
    candidates = streams(data)
    selected_id = (str(selected["fileId"]), str(selected["fileHash"]))
    for stream in candidates:
        if (str(stream["fileId"]), str(stream["fileHash"])) == selected_id:
            return client.dispatch(stream)
    from .errors import SourceChanged
    raise SourceChanged("刷新时选定源已发生变化，将保留旧现场并重新解析新版本")


def episodes(client: NativeClient, tasks: list[Request]) -> Iterator[Episode]:
    """顺序展开去重；client 查询目录，tasks 为任务清单；逐集返回，重复视频参数冲突时报错。"""
    seen = {}
    for task in tasks:
        for episode in catalog(client, task):
            if episode.key in seen:
                if seen[episode.key] != episode.quality:
                    raise ToolError(f"重复视频的 quality 冲突：{episode.key}")
                continue
            seen[episode.key] = episode.quality
            yield episode
