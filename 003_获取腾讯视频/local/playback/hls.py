"""严格解析完整点播 HLS、记录稳定清单身份并恢复已校验分片；网络签名只留内存。"""

from __future__ import annotations

from dataclasses import dataclass
from http.client import IncompleteRead
import math
from pathlib import Path
import re
import time
from threading import Event
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit, parse_qsl

from .transport import Transport
from ..runtime import check_cancel, TaskCancelled
from .errors import AddressExpired, SourceChanged, AuthenticationError, PlaybackError as ToolError
from ..storage import atomic_json, identity_digest as digest, fingerprint, read_json


def file_hash(path: Path, buffer_bytes: int, cancel: Event) -> str:
    """对 path 强制计算 SHA256；buffer_bytes 控制块长，cancel 支持取消；返回摘要。"""
    return fingerprint(path, buffer_bytes, force=True, cancel_event=cancel)["sha256"]


def attributes(text: str) -> dict[str, str]:
    """解析 HLS 属性；text 为冒号后内容；返回键值映射，支持引号内逗号。"""
    return {match[1]: match[2] if match[2] is not None else match[3] for match in re.finditer(r'(?:^|,)([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', text)}


@dataclass(frozen=True)
class Resource:
    """HLS 资源；url 仅驻内存，byte_range 为 offset/length，duration 为秒；stable_path 为已核对的无签名文件名。"""

    url: str
    byte_range: tuple[int, int] | None
    duration: float
    initial: bool = False
    discontinuity: bool = False
    stable_path: str | None = None

    def identity(self) -> dict[str, Any]:
        """返回无签名身份；无参数；核对过的文件名替代含授权的 CDN 路径，其余路径仅记录摘要。"""
        parsed = urlsplit(self.url)
        # 腾讯同一 TS 路径通过 index/brs/bre 指定不同分片；这些语义参数不能随签名一起忽略。
        bounds = sorted((key, value) for key, value in parse_qsl(parsed.query) if key in ("index", "brs", "bre"))
        return {"path_hash": digest(self.stable_path if self.stable_path is not None else parsed.path),
                "query": bounds, "range": self.byte_range, "duration": self.duration,
                "initial": self.initial, "discontinuity": self.discontinuity}

    @property
    def expected_bytes(self) -> int | None:
        """返回明确字节范围的长度；无参数；腾讯查询范围也参与校验，无范围时返回 None。"""
        if self.byte_range:
            return self.byte_range[1]
        query = dict(parse_qsl(urlsplit(self.url).query))
        if "brs" in query or "bre" in query:
            if not {"brs", "bre"}.issubset(query):
                raise ToolError("腾讯分片查询缺少配对的字节范围")
            start, end = int(query["brs"]), int(query["bre"])
            if start < 0 or end < start:
                raise ToolError("腾讯分片查询字节范围非法")
            return end - start + 1
        return None


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
        content = response.read(transport.network["max_response_bytes"] + 1)
    if len(content) > transport.network["max_response_bytes"]:
        raise ToolError("清单超过响应大小上限")
    return content.decode("utf-8-sig")


def playlists(transport: Transport, url: str, *, text: str | None = None, expected: dict | None = None) -> list[Playlist]:
    """展开选定源；transport/url 为传输与基准，text 可提供内嵌清单，expected 约束尺寸；返回视频及音轨。"""
    text = fetch_text(transport, url) if text is None else text
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
    if expected is not None:
        size = f"{expected['width']}x{expected['height']}"
        variants = [value for value in variants if value[1].get("RESOLUTION") == size]
        tolerance = transport.config["verification"]["fps_tolerance"]
        variants = [value for value in variants if "FRAME-RATE" not in value[1]
                    or abs(float(value[1]["FRAME-RATE"]) - float(expected["vfps"])) <= tolerance]
        if not variants:
            raise ToolError("主清单没有与选定画质尺寸一致的变体")
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
    """顺序下载单集资源；initial 为清单，directory 为源版本现场，config 控制重试；refresh 续签，progress 报已完成/总数，cancel 取消；返回本地清单和证据。"""
    steps = download_steps(transport, initial, directory, config, progress, cancel)
    current = None
    try:
        while True:
            try:
                steps.send(current)
            except StopIteration as done:
                return done.value
            current = refresh()
    finally:
        steps.close()


def download_steps(transport: Transport, initial: list[Playlist], directory: Path, config: dict[str, Any],
                   progress: Callable[[int, int], None], cancel: Event,
                   telemetry: Callable[..., None] | None = None):
    """下载 initial 到 directory；progress 报分片进度，telemetry 报字节和平均速度，cancel 可取消；让出续签请求并返回路径及证据。"""
    expected = [playlist.identity for playlist in initial]
    state_path = directory / "segments.json"
    state = read_json(state_path) if state_path.exists() else {"playlists": expected, "completed": {}}
    if state.get("playlists") != expected:
        raise SourceChanged("断点清单身份发生变化")
    total, completed, refreshed = sum(len(playlist.resources) for playlist in initial), 0, 0
    lengths = [resource.expected_bytes for playlist in initial for resource in playlist.resources]
    total_bytes = sum(lengths) if all(length is not None for length in lengths) else None
    downloaded, transferred, transfer_seconds, last_telemetry = 0, 0, 0.0, 0.0

    def report(partial: int = 0, active_seconds: float = 0.0, *, force: bool = False) -> None:
        """按配置限频报告已校验及当前 partial 字节；active_seconds 是当前传输秒数，force 强制更新。"""
        nonlocal last_telemetry
        now = time.monotonic()
        if telemetry is not None and (force or now - last_telemetry >= config["logging"]["progress_interval_seconds"]):
            elapsed = transfer_seconds + active_seconds
            telemetry(downloaded_bytes=downloaded + partial, total_bytes=total_bytes,
                      bytes_per_second=transferred / elapsed if elapsed > 0 and transferred else None)
            last_telemetry = now

    current = initial
    report(force=True)
    for track, playlist in enumerate(initial):
        track_dir = directory / f"track_{track}"
        track_dir.mkdir(parents=True, exist_ok=True)
        for index, resource in enumerate(playlist.resources):
            check_cancel(cancel)
            path, key = track_dir / f"{index:06d}.bin", f"{track}/{index}"
            record = state["completed"].get(key)
            if (record and path.is_file() and path.stat().st_size == record["bytes"]
                    and (resource.expected_bytes is None or record["bytes"] == resource.expected_bytes)
                    and file_hash(path, config["copy_buffer_bytes"], cancel) == record["sha256"]):
                completed += 1
                downloaded += record["bytes"]
                progress(completed, total)
                if completed == total and total_bytes is None:
                    total_bytes = downloaded
                report(force=True)
                continue
            failures = 0
            while True:
                check_cancel(cancel)
                resource = current[track].resources[index]
                temporary = path.with_suffix(".part")
                needs_refresh = False
                try:
                    started = time.perf_counter()
                    headers = {}
                    if resource.byte_range:
                        offset, length = resource.byte_range
                        headers["Range"] = f"bytes={offset}-{offset + length - 1}"
                    with transport.open(resource.url, headers) as response, temporary.open("wb") as output:
                        if resource.byte_range:
                            offset, length = resource.byte_range
                            if response.code != 206 or not str(response.headers.get("Content-Range", "")).startswith(f"bytes {offset}-{offset + length - 1}/"):
                                raise ToolError("服务器未返回请求的精确字节范围")
                        expected_size = resource.expected_bytes
                        declared = response.headers.get("Content-Length")
                        if expected_size is not None and declared is not None and int(declared) != expected_size:
                            raise ToolError("服务器分片长度与腾讯清单字节范围不符")
                        size = 0
                        while True:
                            check_cancel(cancel)
                            if time.perf_counter() - started > config["network"]["timeout_seconds"]:
                                raise TimeoutError("单片传输超过网络配置的总超时")
                            # read1 只等待一次底层读取，避免大缓冲在慢速响应中持续等待整块填满。
                            block = response.read1(config["copy_buffer_bytes"])
                            if not block:
                                break
                            if expected_size is not None and size + len(block) > expected_size:
                                raise ToolError("服务器返回内容超过所选分片范围")
                            output.write(block)
                            size += len(block)
                            transferred += len(block)
                            report(size, time.perf_counter() - started)
                        if not size or (declared is not None and size != int(declared)) or (expected_size is not None and size != expected_size):
                            raise ToolError("分片传输长度不完整")
                    transfer_seconds += time.perf_counter() - started
                    started = None
                    temporary.replace(path)
                    state["completed"][key] = {"bytes": size, "sha256": file_hash(path, config["copy_buffer_bytes"], cancel)}
                    atomic_json(state_path, state)
                    downloaded += size
                    break
                except AddressExpired:
                    if refreshed >= config["download"]["refresh_attempts"]:
                        raise
                    refreshed += 1
                    needs_refresh = True
                except AuthenticationError:
                    raise
                except (ToolError, OSError, IncompleteRead) as error:
                    if isinstance(error, (TaskCancelled, SourceChanged)) or failures >= config["download"]["retries"]:
                        raise
                    failures += 1
                finally:
                    if started is not None:
                        transfer_seconds += time.perf_counter() - started
                    temporary.unlink(missing_ok=True)
                report(force=True)
                if needs_refresh:
                    current = yield "refresh"
                    check_cancel(cancel)
                    if [item.identity for item in current] != expected:
                        raise SourceChanged("续签后的媒体清单发生变化，旧现场已保留") from None
            completed += 1
            progress(completed, total)
            if completed == total and total_bytes is None:
                total_bytes = downloaded
            report(force=True)
    return [local_manifest(playlist, directory / f"track_{track}") for track, playlist in enumerate(initial)], {
        "playlist_hashes": expected, "resources": total, "durations": [item.duration for item in initial], "refreshed": refreshed}
