"""绑定目标正片、画质、音轨、清单与原生授权；敏感源数据仅驻内存。"""

from dataclasses import dataclass, field
import hashlib
import hmac
from urllib.parse import urlsplit

from ..storage import identity_digest
from .authorization import session_key
from .client import PlaybackError, SourceChanged
from .manifest import fetch_media, parse_manifest


@dataclass(repr=False)
class Source:
    """单次已核对媒体来源；description/identity 可保存，segments 中 URL 与 key 禁止序列化。"""

    identity: str
    description: dict
    segments: list[dict] = field(repr=False)
    key: bytes = field(repr=False)
    duration: float


def resolve(client, task: dict, expected: Source | None = None) -> Source:
    """为 client 的 task 解析独立媒体；expected 指定续签来源，返回 Source，变化明确抛错。"""
    body, challenge = client.ups(task["video_id"])
    show, dvd = body.get("show", {}), body.get("dvd", {})
    if (show.get("encodeid") != task["series_id"] or dvd.get("show_videotype") != "正片"
            or int(dvd.get("show_video_ep", 0)) != task["index"]):
        raise PlaybackError("播放响应不是目标节目的指定正片集序")
    profile = client.profile
    streams = [s for s in body["stream"] if s.get("media_type") == "standard" and
               s.get("audio_lang") == profile["audio_language"] and s.get("stream_type") not in profile["adaptive_streams"]]
    order = profile["quality_order"]
    if any(s["stream_type"] not in order for s in streams):
        raise PlaybackError("返回了尚未校验排名的画质，拒绝猜测最高档：" + ",".join(sorted({s["stream_type"] for s in streams if s["stream_type"] not in order})))
    streams.sort(key=lambda s:(int(s["width"])*int(s["height"]), -order.index(s["stream_type"])), reverse=True)
    if len({s["stream_type"] for s in streams}) != len(streams):
        raise PlaybackError("目标语言的画质来源不唯一")
    if len(streams) < task["quality"]:
        raise PlaybackError("请求画质排名不可用，不降档")
    stream = streams[task["quality"] - 1]
    adapter = profile["supported_streams"].get(stream["stream_type"])
    if adapter is None:
        raise PlaybackError("所选画质尚未通过媒体适配验证，不降档")
    if client.config["audio"]["quality"] != 1:
        raise PlaybackError("当前来源只有一档已验证原始音源，请求音质排名不存在")
    if stream.get("drm_type") != "copyrightDRM" or not stream.get("m3u8_url"):
        raise PlaybackError("所选来源保护类型或清单协议不受支持")
    options = dict(profile["media"], timeout_seconds=client.config["network"]["timeout_seconds"])
    raw, final, _ = fetch_media(options, stream["m3u8_url"], options["max_manifest_bytes"])
    manifest = parse_manifest(raw, final)
    if set(manifest["key_methods"]) - {"NONE"}:
        raise PlaybackError("当前不支持标准 HLS 加密与私有保护混合")
    duration = float(body["video"]["seconds"])
    tolerance = client.config["duration_tolerance_seconds"]
    # 下载目录与帧享播放编排的时长口径不同；目录只用作拒绝显著短缺/试看的下限。
    # 所选流、UPS 与完整清单仍须严格在同一容差内相符，不能放宽成品验收阈值。
    if (duration <= 0 or duration+tolerance < task["duration"] or abs(manifest["duration"] - duration) > tolerance
            or abs(float(stream["milliseconds_video"]) / 1000 - duration) > tolerance):
        preview = {k:v for k,v in body.get("preview",{}).items() if type(v) in (int,float,bool)}
        raise PlaybackError(f"媒体时长不匹配：目录={task['duration']}，UPS={duration}，清单={manifest['duration']}，流={stream['milliseconds_video']}ms；preview={preview}")
    description = {k:stream[k] for k in ("stream_type", "width", "height", "size", "drm_type", "audio_lang")}
    description.update(video_id=task["video_id"], series_id=task["series_id"], duration=manifest["duration"],
                       quality=adapter["name"], audio=adapter["audio_codec"].upper(), adapter=adapter)
    structure = [{"duration":s["duration"], "private":s["private"], "discontinuity":s["discontinuity"],
                  "resource":hashlib.sha256(urlsplit(s["url"]).path.encode()).hexdigest()} for s in manifest["segments"]]
    identity = identity_digest({"description":description,"segments":structure,
                               "adapter":profile["restoration"]["library_sha256"]})
    with client.lock:
        key = session_key(profile, {"client":challenge, "server":stream["encryptR_server"],
                                   "copyright":stream["stream_ext"]["copyright_key"]})
    # 以不可逆、带域隔离的身份绑定保护上下文；不会保存密钥或可复用授权材料。
    identity = hmac.new(key,b"youku-ts-pes-v1\0"+identity.encode(),hashlib.sha256).hexdigest()
    result = Source(identity, description, manifest["segments"], key, manifest["duration"])
    if expected is not None and (result.identity != expected.identity or result.key != expected.key):
        raise SourceChanged("续签后的媒体来源或保护上下文发生变化")
    return result
