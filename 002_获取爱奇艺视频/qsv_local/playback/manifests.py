"""识别真实清单内容，将 HLS 或 DASH SegmentList 统一为有序媒体范围；临时地址仅在内存使用。"""

from collections import Counter
import math
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
import xml.etree.ElementTree as ET


def duration_seconds(text: str) -> float:
    """将 DASH 的 ISO 8601 时长 text 转为秒；仅接受有限的时、分、秒格式，返回正浮点数。"""
    match = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", text)
    if match is None:
        raise ValueError("DASH 未提供已适配的点播时长")
    value = sum(float(part or 0) * scale for part, scale in zip(match.groups(), (3600, 60, 1)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("DASH 点播时长必须为正数")
    return value


def ranged_url(url: str, byte_range: str | None) -> str:
    """将 url 与可选的 DASH 闭区间 byte_range 合成现有 [start,end) 请求；返回保留原签名的内存 URL。"""
    if byte_range is None:
        query = parse_qs(urlsplit(url).query)
        if any(len(query.get(key, [])) != 1 for key in ("start", "end")):
            raise ValueError("DASH 媒体未提供明确字节范围")
        return url
    match = re.fullmatch(r"(\d+)-(\d+)", byte_range)
    if match is None or int(match[2]) < int(match[1]):
        raise ValueError("DASH 字节范围无效")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    start, end = int(match[1]), int(match[2]) + 1
    if "start" in query or "end" in query:
        if query.get("start") != [str(start)] or query.get("end") != [str(end)]:
            raise ValueError("DASH 地址与范围属性冲突")
        return url
    return parsed._replace(query=parsed.query + ("&" if parsed.query else "") + urlencode({"start": start, "end": end})).geturl()


def parse_dash(body: bytes, base_url: str, suffixes: list[str]) -> dict:
    """解析 body 的点播 MPD；base_url 为相对地址基准，suffixes 限定媒体域名；返回分轨有序范围和不含地址的媒体声明。"""
    root = ET.fromstring(body)
    if root.tag.rsplit("}", 1)[-1] != "MPD" or root.get("type", "static") != "static":
        raise ValueError("需要完整的 DASH 点播 MPD")
    ns = {"d": "urn:mpeg:dash:schema:mpd:2011"}
    duration = duration_seconds(root.attrib["mediaPresentationDuration"])
    segments, tracks = [], []
    periods = root.findall("d:Period", ns)
    if len(periods) != 1:
        raise ValueError("当前适配要求单 Period DASH 点播")
    for adaptation in periods[0].findall("d:AdaptationSet", ns):
        kind = adaptation.get("contentType")
        reps = adaptation.findall("d:Representation", ns)
        if kind not in ("video", "audio") or len(reps) != 1:
            raise ValueError("DASH 轨道类型或 Representation 数量尚未适配")
        representation = reps[0]
        listing = representation.find("d:SegmentList", ns)
        if listing is None:
            listing = adaptation.find("d:SegmentList", ns)
        if listing is None:
            raise ValueError("DASH 仅适配已核验的 SegmentList")
        initial = listing.find("d:Initialization", ns)
        if initial is None:
            raise ValueError("DASH 缺少初始化范围")
        track_index = len(tracks)
        items = [(initial.get("sourceURL"), initial.get("range"), True)]
        items += [(row.get("media"), row.get("mediaRange"), False) for row in listing.findall("d:SegmentURL", ns)]
        if len(items) < 2:
            raise ValueError("DASH 缺少媒体段")
        for address, bounds, is_init in items:
            if not address:
                raise ValueError("DASH 地址缺失，不猜测媒体模板")
            url = ranged_url(urljoin(base_url, address), bounds)
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or not any(parsed.hostname.endswith(value) for value in suffixes):
                raise ValueError("DASH 媒体不属于已核验的域名")
            query = parse_qs(parsed.query)
            start, end = int(query["start"][0]), int(query["end"][0])
            if start < 0 or end <= start:
                raise ValueError("DASH 分段字节范围无效")
            segments.append({"url": url, "start": start, "end": end, "duration": None, "track": track_index, "initialization": is_init})
        tracks.append({"kind": kind, "codecs": representation.get("codecs", ""), "mime_type": representation.get("mimeType", ""),
                       "bandwidth": int(representation.get("bandwidth", "0")), "segment_count": len(items),
                       "encrypted": bool(adaptation.findall("d:ContentProtection", ns) or representation.findall("d:ContentProtection", ns))})
    counts = Counter(item["kind"] for item in tracks)
    if counts.get("video") != 1 or counts.get("audio", 0) > 1:
        raise ValueError("DASH 视频或音轨数量尚未适配")
    return {"format": "dash", "segments": segments, "tracks": tracks, "duration": duration, "methods": ["cenc"] if any(t["encrypted"] for t in tracks) else []}


def parse_media_manifest(body: bytes, source: dict, profile: dict) -> dict:
    """根据 body 内容识别 source 的媒体清单；profile 提供域名限制，返回统一计划，不根据字段名猜测格式。"""
    if body.lstrip().startswith((b"<", b"\xef\xbb\xbf<")):
        return parse_dash(body, source.get("ml") or source.get("m3u8Url") or "", profile["playback"]["cdn_host_suffixes"])
    from .media import parse_manifest
    plan = parse_manifest(body, source.get("ml", ""), profile["playback"]["resource_host_suffix"])
    return {**plan, "format": "hls", "tracks": []}


def selected_media_plan(body: bytes, source: dict, profile: dict) -> dict:
    """为 body/source 构建最终音视频计划；profile 限定格式，有独立选档音轨时替换 DASH 内置音轨；返回仅内存使用的计划。"""
    plan = parse_media_manifest(body, source, profile)
    audio = source.get("_audio_segments")
    if audio is None:
        return plan
    if not audio or not source.get("_audio_selection", {}).get("native_selection_verified"):
        raise ValueError("独立音轨缺少精确选档证明")
    if plan["format"] == "dash":
        indexes = {index: len([t for t in plan["tracks"][:index] if t["kind"] == "video"]) for index, track in enumerate(plan["tracks"]) if track["kind"] == "video"}
        plan["segments"] = [{**row, "track": indexes[row["track"]]} for row in plan["segments"] if row["track"] in indexes]
        plan["tracks"] = [track for track in plan["tracks"] if track["kind"] == "video"]
    plan["segments"].extend(audio)
    return plan
