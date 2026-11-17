"""按原生音轨声明为每集独立选档；不按文件大小排名，也不将内置 AAC 冒充独立高音质音轨。"""

from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from .manifests import ranged_url


def public_header_value(value, header_name: str) -> str | None:
    """将 SDK 的 value 规范化为 header_name 的纯头值；允许原生完整头行，拒绝换行，返回公开字段或 None。"""
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
        return None
    name, separator, body = value.partition(":")
    return body.strip() if separator and name.strip().casefold() == header_name.casefold() else value.strip()


@dataclass
class AudioSource:
    """保存安全选档证明及私有原生音轨；selected/payload/headers 可能含临时资源，禁止打印或持久化。"""

    report: dict
    selected: dict = field(repr=False)
    payload: dict = field(repr=False)
    headers: dict = field(repr=False)


def audio_groups(candidates: list[dict], settings: dict, language: int | None = None) -> list[list[dict]]:
    """按 settings.rank_fields 对 candidates 分组；language 可限定原生语言编号；返回高到低音质组，同组保留原生顺序。"""
    matched = [row for row in candidates if row.get("_match") in (True, 1) and (language is None or row.get("lid") == language)]
    if candidates and not matched:
        raise ValueError("原生音轨没有符合指定视频语言的有效匹配声明，不猜测切换配音")
    pool = matched
    groups = {}
    for row in pool:
        key = []
        for name in settings["rank_fields"]:
            value = row.get(name)
            if type(value) is not int or value < 0:
                raise ValueError("原生音轨缺少可比较的声明档位，不按媒体体积猜测排名")
            key.append(value)
        groups.setdefault(tuple(key), []).append(row)
    # 同档多份音轨的顺序来自原生节目上下文，AID 哈希的字典序没有品质含义。
    return [sorted(groups[key], key=lambda row: not row.get("_selected", False)) for key in sorted(groups, reverse=True)]


def select_native_audio(profile: dict, video: dict, initial, rank: int) -> AudioSource:
    """从 initial 的原生候选为 video 选第 rank 组；profile 提供请求结构与排序规则，返回精确匹配的私有音轨资源。"""
    from .source import request_source
    if type(rank) is not int or rank <= 0:
        raise ValueError("audio.quality 必须为正整数，1 表示最高音质")
    candidates = initial.payload.get("data", {}).get("program", {}).get("audio", [])
    language = initial.selected.get("lid")
    if type(language) is not int or language <= 0:
        raise ValueError("原生视频缺少明确语言编号，不猜测配音")
    groups = audio_groups(candidates, profile["playback"]["audio_selection"], language)
    if rank > len(groups):
        raise ValueError("原生响应没有请求排名的独立音轨，不用默认 AAC 替代")
    desired = groups[rank - 1][0]
    codec_types = profile["playback"]["audio_selection"]["codec_types"]
    if desired.get("cf") not in codec_types:
        raise ValueError("最高音轨的原生编码尚未核验，不用较低档替代")
    result = initial
    if desired.get("_selected") is not True or not desired.get("fs"):
        extra = {"abid": desired["bid"]}
        if type(desired.get("aat")) is int:
            extra["application_type"] = desired["aat"]
        # 预读接口将请求的 bitstream 同时用于 abid；此独立请求只获取音轨，主视频继续保留初次的最高档来源。
        override = {"bitstream": desired["bid"], "audio_language": desired["lid"], "audio_type": codec_types[desired["cf"]],
                    "audio_channel_type": desired["ct"], "audio_extend_info": extra}
        result = request_source(profile, video, override, require_video=False)
    choices = [row for row in result.payload.get("data", {}).get("program", {}).get("audio", []) if row.get("_selected") is True]
    if len(choices) != 1 or choices[0].get("aid") != desired["aid"]:
        available = [{key: row.get(key) for key in ("aid", "bid", "ct", "_selected")} for row in result.payload.get("data", {}).get("program", {}).get("audio", [])]
        raise RuntimeError(f"原生 SDK 未精确返回请求音轨，不接受替代；请求 AID={desired['aid']}，实际声明={available}")
    selected = choices[0]
    # ispre 是原生片源标记，不能当作 DRM.preview；只使用明确的试看字段及完整分段时长判断。
    drm = selected.get("drm")
    if selected.get("isPreview") is True or isinstance(drm, dict) and (drm.get("code") != "A00000" or drm.get("preview") is not False):
        raise RuntimeError("最高音轨明确标记为未授权或试听，不将试听当成完整音轨")
    if not isinstance(selected.get("fs"), list) or not selected["fs"]:
        raise RuntimeError("原生最高音轨缺少实际媒体分段")
    report = {"rank": rank, "source_aid": selected["aid"], "tier": [selected[key] for key in profile["playback"]["audio_selection"]["rank_fields"]],
              "native_selection_verified": True, "available_declared_groups": len(groups),
              "codec": selected["cf"], "channel_type": selected["ct"], "declared_bytes": selected["asize"],
              "authorization": {"source": "native_full_resource_response", "ispre": selected.get("ispre"), "isPreview": selected.get("isPreview"),
                                "drm_code": drm.get("code") if isinstance(drm, dict) else None, "drm_preview": drm.get("preview") if isinstance(drm, dict) else None}}
    return AudioSource(report, selected, result.payload, result.headers)


def audio_segments(profile: dict, selected: dict, video_seconds: float, dispatch_base: str) -> list[dict]:
    """将 selected.fs 转换为完整 AMP4；profile 提供边界，video_seconds 校验时间，dispatch_base 为原生调度前缀；全零 d 最终按实测时长校验。"""
    rows = selected.get("fs")
    if not isinstance(rows, list) or not rows:
        raise ValueError("所选最高音轨没有媒体分段")
    if any(type(row.get("i")) is not int or type(row.get("b")) is not int or row["b"] <= 0 for row in rows):
        raise ValueError("独立音轨的序号或声明字节数无效")
    rows = sorted(rows, key=lambda row: row["i"])
    if len({row["i"] for row in rows}) != len(rows):
        raise ValueError("独立音轨分段序号重复")
    if sum(row["b"] for row in rows) != selected["asize"]:
        raise ValueError("独立音轨分段字节数与音轨声明不一致")
    durations = [row.get("d") for row in rows]
    if any(type(value) not in (int, float) or value < 0 for value in durations):
        raise ValueError("独立音轨缺少有效分段时长")
    if any(durations) and abs(sum(durations) / 1000 - video_seconds) > profile["playback"]["media_download"]["duration_tolerance_seconds"]:
        raise ValueError("最高音轨不是与正片等长的完整资源")
    result = []
    for index, row in enumerate(rows):
        address = row.get("l")
        if not isinstance(address, str) or not address:
            raise ValueError("独立音轨调度地址缺失")
        base = dispatch_base
        # fs.l 是以 /v0 开始的资源相对路径；原生 dd 的 /videos 前缀必须保留，不能按站点绝对路径 urljoin。
        logical_url = address if urlsplit(address).scheme else base.rstrip("/") + "/" + address.lstrip("/")
        play = row.get("play", {}).get("ts", {})
        # play.ts.d可能是客户端缓存的路由；每个物理块通过自身完整签名的HTTPS调度地址重新解析。
        url = logical_url
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not any(parsed.hostname.endswith(value) for value in profile["playback"]["cdn_host_suffixes"]):
            raise ValueError("独立音轨地址不属于已核验的域名")
        query = parse_qs(parsed.query)
        url = ranged_url(url, None if "start" in query or "end" in query else f"0-{row['b'] - 1}")
        bounds = parse_qs(urlsplit(url).query)
        start, end = int(bounds["start"][0]), int(bounds["end"][0])
        if end - start != row["b"]:
            raise ValueError("独立音轨地址范围与声明大小不一致")
        public_headers = {}
        common = rows[0].get("play", {}).get("ts", {})
        agent = public_header_value(play.get("ua") or common.get("ua"), "User-Agent")
        if isinstance(agent, str) and agent:
            public_headers["User-Agent"] = agent
        reference = public_header_value(play.get("re") or common.get("re"), "Referer")
        if isinstance(reference, str):
            referer = urlsplit(reference)
            if referer.scheme in ("http", "https") and referer.hostname in profile["network"]["allowed_hosts"]:
                public_headers["Referer"] = referer.geturl()
        result.append({"url": url, "headers": public_headers, "range_style": "whole", "duration": row["d"] / 1000 if row["d"] else None, "audio_asset": True, "initialization": index == 0,
                       "start": start, "end": end, "resource_index": row["i"]})
    return result
