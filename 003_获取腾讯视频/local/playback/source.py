"""固定逐集画质、音质及稳定来源身份；签名刷新不能偷偷换档或复用不同版本分片。"""

from dataclasses import dataclass, replace
import math
import re
from urllib.parse import urlsplit

from ..storage import identity_digest
from .audio import AudioOption, AudioSelector, options
from .client import NativeClient, formats, video_info
from .errors import PlaybackError, SourceChanged
from .hls import Playlist, playlists


@dataclass
class Resolved:
    """已选来源；description 为可落盘元数据，manifests 的地址仅在内存，audio 为所选档位，identity 为稳定摘要。"""

    description: dict
    manifests: list[Playlist]
    audio: AudioOption
    identity: str


def describe_video(value: dict, video: dict) -> dict:
    """从选定 value 和原生 video 提取稳定元数据；返回不含账号、签名及媒体地址的字典。"""
    keys = ("id", "name", "sname", "resolution", "width", "height", "vfps", "vcodec", "audio",
            "bandwidth", "audiobandwidth", "fs", "videoenh", "formatdefn", "zqe", "audioscore", "aliases")
    return {"format": {key: value[key] for key in keys if key in value},
            "file_name": video["fn"], "file_md5": video.get("fmd5"),
            "duration_seconds": float(video["td"]), "source_type": "url"}


def selected_format(data: dict, expected: dict, fps_tolerance: float) -> dict:
    """核对 data 与 expected 的格式身份，fps_tolerance 为允许帧率差；返回实际档位，降档时拒绝。"""
    actual = [value for value in data["fl"]["fi"] if value.get("sl") == 1]
    if not actual or {value["id"] for value in actual} != {expected["id"]}:
        raise PlaybackError("服务端未返回指定画质，拒绝使用降档或其他码流")
    video = video_info(data)
    if not math.isfinite(float(video["fps"])) or float(video["fps"]) <= 0:
        raise PlaybackError("实际播放源帧率无效")
    if (int(video["vw"]) != int(expected["width"]) or int(video["vh"]) != int(expected["height"])
            or abs(float(video["fps"]) - float(expected["vfps"])) > fps_tolerance):
        raise PlaybackError("实际播放源尺寸或帧率与选定画质不符")
    canonical = next((value for value in actual if value["name"] == expected["name"]), actual[0])
    return {**canonical, "aliases": expected.get("aliases", [])}


def stable_media(media: list[Playlist], filename: str) -> list[Playlist]:
    """为 media 中与 filename 对应的腾讯虚拟 TS 段核对稳定文件名；返回忽略 CDN 授权路径的新清单。"""
    if not isinstance(filename, str) or "/" in filename or "\\" in filename or not filename.endswith(".ts"):
        return media
    stem = re.escape(filename[:-3])
    # 虚拟段前缀是 0+十进制序号（00、010、0100），并非固定两位。
    pattern = re.compile(rf"(?:0\d+_)?{stem}\.\d+\.ts")
    result = []
    for playlist in media:
        resources = []
        for resource in playlist.resources:
            basename = urlsplit(resource.url).path.rsplit("/", 1)[-1]
            # 只规范化与 getinfo.fn 精确对应的物理文件；未知目录结构保留原身份，不按任意 basename 合并。
            resources.append(replace(resource, stable_path=basename) if pattern.fullmatch(basename) else resource)
        result.append(replace(playlist, resources=resources))
    return result


def full_video(data: dict, settings: dict) -> bool:
    """按 settings 检查 data 的正片状态；preview 是预设试玩时长，须结合 exem 判断；返回是否完整授权。"""
    preview, extra_error = data.get("preview", 0), data.get("exem", 0)
    try:
        preview, extra_error = float(preview), float(extra_error)
    except (TypeError, ValueError):
        raise PlaybackError("播放响应的试玩或附加状态非法") from None
    if not math.isfinite(preview) or not math.isfinite(extra_error):
        raise PlaybackError("播放响应的试玩或附加状态非法")
    # 官方播放器只有 exem>0 或 vi.st=试玩状态时才应用 preview，正常正片也可能返回预设时长。
    return not (extra_error > 0 and preview > 0) and video_info(data).get("st") in settings["full_video_states"]


def resolve(client: NativeClient, task: dict, selector: AudioSelector,
            *, expected: Resolved | None = None) -> Resolved:
    """为 client/task 选择并验证来源；selector 控制音质，expected 固定续签来源；返回完整清单与身份。"""
    cid, vid = task["series_id"], task["video_id"]
    settings = client.profile["playback"]
    fps_tolerance = client.config["verification"]["fps_tolerance"]
    if expected is None:
        initial = client.source(cid, vid)
        choices = formats(initial, settings)
        authorized = 0
        for candidate in choices:
            data = client.source(cid, vid, candidate["name"])
            if not full_video(data, settings):
                # 服务端明确未授予正片播放；不把预览源算作账号可获取档位。
                continue
            current = selected_format(data, candidate, fps_tolerance)
            authorized += 1
            if authorized == task["quality"]:
                break
        else:
            raise PlaybackError(f"账号可完整获取的档位不足，无法选择第 {task['quality']} 档")
        video = video_info(data)
        available_audio = options(data, video, current)
        audio = selector.select(cid, available_audio)
    else:
        current = expected.description["format"]
        audio = expected.audio
        data = client.source(cid, vid, current["name"], audio.request)
        if not full_video(data, settings):
            raise PlaybackError("续签后服务端未授予所选档位完整播放权限")
        current = selected_format(data, current, fps_tolerance)
        video = video_info(data)
        available_audio = options(data, video, current)
        if not any(item.id == audio.id and item.kind == audio.kind and item.authorized and item.downloadable
                   for item in available_audio):
            raise SourceChanged("续签后所选音质不可用，保留现场并重新选择")
    if audio.request:
        data = client.source(cid, vid, current["name"], audio.request)
        if not full_video(data, settings):
            raise PlaybackError("服务端未授予所选音质的完整播放权限")
        current = selected_format(data, current, fps_tolerance)
        video = video_info(data)
        if str(video.get("audio_track")) != audio.id:
            raise PlaybackError("服务端未确认所选独立音质，拒绝误标成品")
    description = describe_video(current, video)
    total = video.get("totalduration")
    if total is not None and abs(float(total) / 1000 - description["duration_seconds"]) > client.config["duration_tolerance_seconds"]:
        raise PlaybackError("实际播放源时长与完整正片元数据不一致")
    description["audio"] = audio.describe()
    description["audio_options"] = [value.describe() for value in available_audio]
    priority = settings["delivery_priority"]
    addresses = sorted(video["ul"]["ui"], key=lambda value: priority.index(value["vt"])
                       if value.get("vt") in priority else len(priority))
    if not addresses or urlsplit(addresses[0].get("url", "")).scheme not in ("https", "http"):
        raise PlaybackError("所选来源没有有效 HLS 地址")
    address = addresses[0]["url"]
    if addresses[0].get("hls"):
        address += addresses[0]["hls"].get("pt", "")
    content = video["ul"].get("m3u8")
    # 内嵌清单可能带原先 LTS 节点的绝对 URI，选用标准 CDN 时需读取该 CDN 的完整清单。
    if isinstance(content, str) and any(urlsplit(line).hostname for line in content.splitlines()
                                        if line and not line.startswith("#")):
        content = None
    media = playlists(client.transport, address, text=content if isinstance(content, str) and content.startswith("#EXTM3U") else None,
                      expected=current)
    media = stable_media(media, video["fn"])
    tolerance = client.config["duration_tolerance_seconds"]
    if any(abs(item.duration - description["duration_seconds"]) > tolerance for item in media):
        raise PlaybackError("媒体清单不是完整正片，或独立音轨时长不一致")
    identity = identity_digest({"series_id": cid, "video_id": vid,
                                "source": {key: value for key, value in description.items() if key != "audio_options"},
                                "manifests": [item.identity for item in media]})
    result = Resolved(description, media, audio, identity)
    if expected is not None and identity != expected.identity:
        raise SourceChanged("签名刷新后来源版本发生变化，旧分片现场保留")
    return result
