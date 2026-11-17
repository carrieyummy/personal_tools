"""顺序流式下载单集分段，登记可靠断点；剧集间并发由外部调度器控制。"""

import hashlib
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler, Request

from .runtime import check_cancel
from .errors import safe_message
from .storage import atomic_json, fingerprint, read_json
from .youku.client import PlaybackError
from .youku.manifest import MediaRedirect, require_media_url


class SegmentLimitError(PlaybackError):
    """声明或实际分段超过配置上限；重复下载不能修复，直接保留断点并明确失败。"""


def transfer(url: str, path: Path, config: dict, expected_size: int | None, cancel) -> dict:
    """将内存 url 流式写入新 path；config 控制缓冲/限制，expected_size 为声明长度，cancel 可取消；返回摘要。"""
    options = config["playback"]["media"]
    require_media_url(url, options["allowed_domains"], options["allowed_schemes"])
    proxy = config["network"]["proxy"]
    opener = build_opener(ProxyHandler({"http":proxy,"https":proxy} if proxy else {}),
                         MediaRedirect(options["allowed_domains"], options["allowed_schemes"]))
    request = Request(url, headers={"User-Agent":options["user_agent"], "Accept-Encoding":"identity"})
    temporary = path.with_suffix(".partial")
    digest, size, started = hashlib.sha256(), 0, time.monotonic()
    try:
        check_cancel(cancel)
        with opener.open(request, timeout=config["network"]["timeout_seconds"]) as response, temporary.open("wb") as output:
            if response.status != 200:
                raise PlaybackError("分段返回非完整响应")
            length = response.headers.get("Content-Length")
            if length is not None and not length.isdecimal():
                raise PlaybackError("分段响应长度非法")
            length = int(length) if length is not None else None
            if length is not None and length > options["max_segment_bytes"]:
                raise SegmentLimitError(f"分段响应长度 {length} 字节超过 playback.media.max_segment_bytes={options['max_segment_bytes']}")
            while True:
                check_cancel(cancel)
                data = response.read1(config["copy_buffer_bytes"])
                if not data:
                    break
                size += len(data)
                if size > options["max_segment_bytes"]:
                    raise SegmentLimitError(f"分段已接收 {size} 字节超过 playback.media.max_segment_bytes={options['max_segment_bytes']}")
                if time.monotonic() - started > config["network"]["timeout_seconds"]:
                    raise PlaybackError(f"分段总传输时间超过 {config['network']['timeout_seconds']} 秒")
                output.write(data)
                digest.update(data)
        if not size or (length is not None and size != length) or (expected_size is not None and size != expected_size):
            raise PlaybackError(f"分段长度不符：实收={size}，HTTP声明={length}，清单声明={expected_size}")
        temporary.replace(path)
        return {"bytes":size,"sha256":digest.hexdigest()}
    finally:
        temporary.unlink(missing_ok=True)


def download(source, work: Path, config: dict, cancel, refresh, notify) -> tuple[list[Path], dict]:
    """下载 source 至 work；refresh 返回同源新签名，notify 接收真实字节进度；返回分段路径和脱敏证据。"""
    steps = download_steps(source, work, config, cancel, notify)
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


def download_steps(source, work: Path, config: dict, cancel, notify):
    """按 config 下载 source 至 work；cancel 可取消，notify 接收进度；让出续签请求，send 接收同源 Source，返回路径及证据。"""
    ledger_path = work / "segments.json"
    ledger = read_json(ledger_path) if ledger_path.exists() else {"identity":source.identity,"segments":{}}
    if ledger.get("identity") != source.identity or not isinstance(ledger.get("segments"),dict):
        raise PlaybackError("断点来源记录不匹配")
    folder = work / "segments"
    folder.mkdir(exist_ok=True)
    paths, downloaded, reused, transferred = [], 0, 0, 0
    started = time.monotonic()
    declared = [s["private"].get("FILESIZE") for s in source.segments]
    for index, size in enumerate(declared):
        if isinstance(size, int) and size > config["playback"]["media"]["max_segment_bytes"]:
            raise SegmentLimitError(f"第 {index + 1}/{len(declared)} 段声明 {size} 字节超过 "
                                    f"playback.media.max_segment_bytes={config['playback']['media']['max_segment_bytes']}")
    total = sum(declared) if all(isinstance(x,int) and x > 0 for x in declared) else None
    refreshed = 0
    current = source
    for index, segment in enumerate(current.segments):
        check_cancel(cancel)
        path = folder / f"{index:06d}.ts"
        record = ledger["segments"].get(str(index))
        valid = False
        if path.exists() and isinstance(record,dict):
            actual = fingerprint(path,config["copy_buffer_bytes"],force=True,cancel_event=cancel)
            valid = actual["size_bytes"] == record.get("bytes") and actual["sha256"] == record.get("sha256")
            valid = valid and (declared[index] is None or actual["size_bytes"] == declared[index])
        if valid:
            reused += 1
        else:
            failures = 0
            while True:
                try:
                    record = transfer(current.segments[index]["url"],path,config,declared[index],cancel)
                    transferred += record["bytes"]
                    break
                except SegmentLimitError:
                    raise
                except HTTPError as error:
                    if error.code in (403,410) and refreshed < config["download"]["refresh_attempts"]:
                        current = yield "refresh"
                        check_cancel(cancel)
                        refreshed += 1
                        continue
                    if failures == config["download"]["retries"]:
                        raise PlaybackError(f"第 {index + 1}/{len(declared)} 段 HTTP 失败：{error.code}") from None
                    failures += 1
                except (URLError, TimeoutError, OSError, PlaybackError) as error:
                    if failures == config["download"]["retries"]:
                        raise PlaybackError(f"第 {index + 1}/{len(declared)} 段传输重试耗尽：" + safe_message(error)) from None
                    failures += 1
            ledger["segments"][str(index)] = record
            atomic_json(ledger_path,ledger,config["storage"])
        downloaded += record["bytes"]
        paths.append(path)
        notify(downloaded_bytes=downloaded,total_bytes=total,
               bytes_per_second=transferred / max(time.monotonic()-started,0.001),
               progress=(downloaded*100/total) if total else (index+1)*100/len(source.segments))
    return paths,{"bytes":downloaded,"reused_segments":reused,"segments":len(paths),"refreshes":refreshed}
