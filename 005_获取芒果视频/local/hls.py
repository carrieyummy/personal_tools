"""严格解析完整点播 HLS、记录稳定清单身份并恢复已校验分片；网络签名只留内存。"""

from __future__ import annotations

from dataclasses import dataclass
from http.client import IncompleteRead
import math
from pathlib import Path
import re
from threading import Event
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from .client import Transport, check_cancel
from .errors import AddressExpired, SourceChanged, ToolError
from .storage import atomic_json, digest, file_hash, read_json


def attributes(text: str) -> dict[str, str]:
    """解析 HLS 属性；text 为冒号后内容；返回键值映射，支持引号内逗号。"""
    return {match[1]: match[2] if match[2] is not None else match[3] for match in re.finditer(r'(?:^|,)([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', text)}


@dataclass(frozen=True)
class Resource:
    """HLS 资源；url 只驻内存，byte_range 为可选 offset/length，duration 为秒，initial/discontinuity 标记结构。"""

    url: str
    byte_range: tuple[int, int] | None
    duration: float
    initial: bool = False
    discontinuity: bool = False

    def identity(self) -> dict[str, Any]:
        """返回无签名身份；无参数；路径仅以哈希记录，忽略 CDN 主机和临时查询参数。"""
        return {"path_hash": digest(urlsplit(self.url).path), "range": self.byte_range, "duration": self.duration,
                "initial": self.initial, "discontinuity": self.discontinuity}


@dataclass
class Playlist:
    """完整媒体清单；resources 为顺序资源，sequence 为起始序号，target 为目标时长。"""

    resources: list[Resource]
    sequence: int
    target: int

    @property
    def duration(self) -> float:
        """返回完整媒体秒数；无参数；初始化片段不计时。"""
        return sum(resource.duration for resource in self.resources)

    @property
    def identity(self) -> str:
        """返回稳定清单指纹；无参数；覆盖时序、分段路径及范围，不含签名。"""
        return digest({"sequence": self.sequence, "target": self.target, "resources": [resource.identity() for resource in self.resources]})


def parse_media(text: str, url: str) -> Playlist:
    """解析点播媒体清单；text 为 UTF8 内容，url 为基准；返回完整清单，直播、加密、未知媒体结构明确失败。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U" or "#EXT-X-ENDLIST" not in lines:
        raise ToolError("需要带 ENDLIST 的完整点播清单")
    resources, sequence, target = [], 0, 0
    duration, pending_range, discontinuity = None, None, False
    last_range_url, last_end = None, 0
    for line in lines[1:]:
        if line.startswith("#EXT-X-KEY:"):
            if attributes(line.split(":", 1)[1]).get("METHOD") != "NONE":
                raise ToolError("该源采用加密 HLS，当前适配未验证其授权还原机制")
        elif line.startswith("#EXT-X-MAP:"):
            info = attributes(line.split(":", 1)[1])
            if "URI" not in info:
                raise ToolError("初始化片段缺少 URI")
            bounds = parse_range(info["BYTERANGE"], 0) if "BYTERANGE" in info else None
            resources.append(Resource(urljoin(url, info["URI"]), bounds, 0, True))
        elif line.startswith("#EXTINF:"):
            if duration is not None:
                raise ToolError("清单分片时长未匹配资源")
            duration = float(line.split(":", 1)[1].split(",")[0])
            if not math.isfinite(duration) or duration <= 0:
                raise ToolError("清单分片时长无效")
        elif line.startswith("#EXT-X-BYTERANGE:"):
            pending_range = line.split(":", 1)[1]
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            target = int(line.split(":", 1)[1])
        elif line == "#EXT-X-DISCONTINUITY":
            discontinuity = True
        elif line.startswith(("#EXT-X-GAP", "#EXT-X-PART", "#EXT-X-STREAM-INF", "#EXT-X-I-FRAMES-ONLY")):
            raise ToolError("清单包含不支持的缺片、低延迟或非完整媒体结构")
        elif not line.startswith("#"):
            if duration is None:
                raise ToolError("媒体清单资源缺少 EXTINF")
            address = urljoin(url, line)
            if pending_range and "@" not in pending_range and address != last_range_url:
                raise ToolError("隐式字节范围不能切换资源")
            bounds = parse_range(pending_range, last_end) if pending_range else None
            resources.append(Resource(address, bounds, duration, False, discontinuity))
            if bounds:
                last_range_url, last_end = address, sum(bounds)
            duration, pending_range, discontinuity = None, None, False
    if duration is not None or pending_range or not resources or not any(not item.initial for item in resources) or target <= 0:
        raise ToolError("媒体清单不完整")
    return Playlist(resources, sequence, target)


def parse_range(value: str, implicit: int) -> tuple[int, int]:
    """解析字节范围；value 为 length[@offset]，implicit 为上段结束位置；返回 offset/length。"""
    parts = value.split("@")
    length, offset = int(parts[0]), int(parts[1]) if len(parts) == 2 else implicit
    if length <= 0 or offset < 0 or len(parts) > 2:
        raise ToolError("HLS 字节范围无效")
    return offset, length


def fetch_text(transport: Transport, url: str) -> str:
    """读取清单；transport 提供网络和大小限制，url 仅留内存；返回 UTF8 文本，超限报错。"""
    with transport.open(url) as response:
        content = response.read(transport.network["max_json_bytes"] + 1)
    if len(content) > transport.network["max_json_bytes"]:
        raise ToolError("清单超过响应大小上限")
    return content.decode("utf-8-sig")


def playlists(transport: Transport, url: str) -> list[Playlist]:
    """展开选定源；transport 为网络，url 为清单；返回视频和可选独立音轨清单，主清单只选最高变体。"""
    text = fetch_text(transport, url)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants, audio = [], []
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA:"):
            info = attributes(line.split(":", 1)[1])
            if info.get("TYPE") == "AUDIO":
                audio.append(info)
        if line.startswith("#EXT-X-STREAM-INF:"):
            info = attributes(line.split(":", 1)[1])
            if index + 1 >= len(lines) or lines[index + 1].startswith("#"):
                raise ToolError("主清单变体缺少 URI")
            variants.append((int(info["BANDWIDTH"]), info, urljoin(url, lines[index + 1])))
    if not variants:
        return [parse_media(text, url)]
    _, info, address = max(variants, key=lambda variant: variant[0])
    result = [parse_media(fetch_text(transport, address), address)]
    group = [track for track in audio if track.get("GROUP-ID") == info.get("AUDIO") and "URI" in track]
    for track in group:
        address = urljoin(url, track["URI"])
        result.append(parse_media(fetch_text(transport, address), address))
    if info.get("AUDIO") and not group and not any(track.get("GROUP-ID") == info["AUDIO"] and "URI" not in track for track in audio):
        raise ToolError("主清单引用的音轨组不存在")
    return result


def local_manifest(playlist: Playlist, directory: Path) -> Path:
    """生成仅含本地文件的清单；playlist 为完整结构，directory 包含编号分片；返回本地 m3u8 路径。"""
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{playlist.target}", f"#EXT-X-MEDIA-SEQUENCE:{playlist.sequence}", "#EXT-X-PLAYLIST-TYPE:VOD"]
    for index, resource in enumerate(playlist.resources):
        name = f"{index:06d}.bin"
        if resource.initial:
            lines.append(f'#EXT-X-MAP:URI="{name}"')
        else:
            if resource.discontinuity:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.extend([f"#EXTINF:{resource.duration:.9f},", name])
    lines.append("#EXT-X-ENDLIST")
    path = directory / "local.m3u8"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def download(transport: Transport, initial: list[Playlist], directory: Path, config: dict[str, Any], refresh: Callable[[], list[Playlist]],
             progress: Callable[[int, int], None], cancel: Event) -> tuple[list[Path], dict[str, Any]]:
    """顺序下载单集资源；initial 为清单，directory 为源版本现场，config 控制重试；refresh 续签，progress 报已完成/总数，cancel 取消。

    返回本地清单和证据；bytes 为所有原始分片实际字节数，包含已校验断点和初始化片段，不含清单及元数据。
    """
    expected = [playlist.identity for playlist in initial]
    state_path = directory / "segments.json"
    state = read_json(state_path, {"playlists": expected, "completed": {}})
    if state.get("playlists") != expected:
        raise SourceChanged("断点清单身份发生变化")
    total, completed, refreshed = sum(len(playlist.resources) for playlist in initial), 0, 0
    downloaded_bytes = 0
    current = initial
    for track, playlist in enumerate(initial):
        track_dir = directory / f"track_{track}"
        track_dir.mkdir(parents=True, exist_ok=True)
        for index, resource in enumerate(playlist.resources):
            check_cancel(cancel)
            path, key = track_dir / f"{index:06d}.bin", f"{track}/{index}"
            record = state["completed"].get(key)
            if record and path.is_file() and path.stat().st_size == record["bytes"] and file_hash(path, config["download"]["buffer_bytes"], cancel) == record["sha256"]:
                downloaded_bytes += record["bytes"]
                completed += 1
                progress(completed, total)
                continue
            failures = 0
            while True:
                check_cancel(cancel)
                resource = current[track].resources[index]
                temporary = path.with_suffix(".part")
                try:
                    headers = {}
                    if resource.byte_range:
                        offset, length = resource.byte_range
                        headers["Range"] = f"bytes={offset}-{offset + length - 1}"
                    with transport.open(resource.url, headers) as response, temporary.open("wb") as output:
                        if resource.byte_range:
                            offset, length = resource.byte_range
                            if response.code != 206 or not str(response.headers.get("Content-Range", "")).startswith(f"bytes {offset}-{offset + length - 1}/"):
                                raise ToolError("服务器未返回请求的精确字节范围")
                        size = 0
                        while block := response.read(config["download"]["buffer_bytes"]):
                            check_cancel(cancel)
                            output.write(block)
                            size += len(block)
                        declared = response.headers.get("Content-Length")
                        if not size or (declared is not None and size != int(declared)) or (resource.byte_range and size != resource.byte_range[1]):
                            raise ToolError("分片传输长度不完整")
                    temporary.replace(path)
                    state["completed"][key] = {"bytes": size, "sha256": file_hash(path, config["download"]["buffer_bytes"], cancel)}
                    atomic_json(state_path, state, config["logging"])
                    break
                except AddressExpired:
                    if refreshed >= config["download"]["refresh_attempts"]:
                        raise
                    refreshed += 1
                    current = refresh()
                    if [item.identity for item in current] != expected:
                        raise SourceChanged("续签后的媒体清单发生变化，旧现场已保留") from None
                except (ToolError, OSError, IncompleteRead) as error:
                    from .errors import Cancelled
                    if isinstance(error, (Cancelled, SourceChanged)) or failures >= config["download"]["retries"]:
                        raise
                    failures += 1
                finally:
                    temporary.unlink(missing_ok=True)
            downloaded_bytes += size
            completed += 1
            progress(completed, total)
    return [local_manifest(playlist, directory / f"track_{track}") for track, playlist in enumerate(initial)], {
        "playlist_hashes": expected, "resources": total, "bytes": downloaded_bytes,
        "durations": [item.duration for item in initial], "refreshed": refreshed}
