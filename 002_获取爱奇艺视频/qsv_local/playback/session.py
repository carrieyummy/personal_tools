"""捕获并重放当前登录播放会话；临时凭据、地址及响应只在内存中处理。"""

from datetime import datetime
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from threading import Event
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from ..storage import atomic_json
from .configuration import read_profile
from .metadata import linked_videos
from .network import open_network


def playback_modules(profile: dict) -> tuple[int, dict]:
    """核验 profile 指定播放器及两份模块摘要；返回唯一 PID 和模块路径映射，版本变化时失败。"""
    import psutil

    settings, root = profile["playback"], profile["client_root"]
    candidates = []
    for process in psutil.process_iter(["name", "exe"]):
        try:
            if process.info["name"] != settings["process_name"] or not process.info["exe"]:
                continue
            if not Path(process.info["exe"]).resolve().is_relative_to(root):
                continue
            loaded = {Path(item.path).name.casefold(): Path(item.path).resolve()
                      for item in process.memory_maps(grouped=True)}
            modules = {key: loaded.get(settings[key]["module"].casefold()) for key in ("qtp", "player")}
            if all(modules.values()):
                candidates.append((process.pid, modules))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if len(candidates) != 1:
        raise RuntimeError("需要已登录且已初始化的原生播放器；请在客户端打开任意一集正片并暂停，保持窗口打开")
    pid, modules = candidates[0]
    for key, path in modules.items():
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != settings[key]["sha256"]:
            raise RuntimeError("播放模块未经当前 ABI 核验，停止观察")
    return pid, modules


def is_target_dash(url: str, settings: dict, tvid: str) -> bool:
    """校验 url 是否为 settings 中的 DASH 端点且目标编号恰为 tvid；返回布尔值。"""
    parsed = urlsplit(url)
    return (parsed.scheme in ("http", "https") and parsed.hostname == settings["dash_host"]
            and parsed.path == settings["dash_path"] and parse_qs(parsed.query).get("tvid") == [tvid])


def capture_session(profile: dict, video: dict, request_override: dict | None = None) -> list[dict]:
    """按 profile 通过 SDK 请求 video，request_override 可指定已发现的画质；返回内存请求并清理本次预读，不切换界面。"""
    import frida

    settings = deepcopy(profile["playback"])
    if request_override is not None:
        settings["request"].update(request_override)
    pid, modules = playback_modules(profile)
    captured, errors, complete = {}, [], Event()

    def message(value, data):
        """接收 Frida 的 value 及二进制 data；只在内存归并有界响应，不打印原始消息。"""
        if value["type"] == "error":
            errors.append("原生观察脚本发生错误")
            complete.set()
            return
        item = value.get("payload", {})
        identifier = item.get("id")
        if item.get("event") == "request":
            if identifier not in captured and len(captured) >= settings["max_requests"]:
                errors.append("播放会话请求数量超出配置上限")
                complete.set()
                return
            captured[identifier] = {"url": item["url"], "headers": item["headers"], "body": bytearray(), "complete": False}
        elif identifier in captured:
            record = captured[identifier]
            if item.get("event") == "body" and data:
                if len(record["body"]) + len(data) > settings["max_body_bytes"]:
                    errors.append("播放响应超出配置上限")
                    complete.set()
                else:
                    record["body"].extend(data)
            elif item.get("event") == "complete":
                record["complete"] = True
                if is_target_dash(record["url"], settings, video["tvid"]):
                    complete.set()

    session, script = None, None
    try:
        session = frida.attach(pid)
        script = session.create_script(Path(__file__).with_suffix(".js").read_text(encoding="utf-8"))
        script.on("message", message)
        script.load()
        loaded = script.exports_sync.install(settings)
        if any(Path(loaded[key]).resolve() != modules[key] for key in modules):
            raise RuntimeError("播放模块路径发生变化")
        script.exports_sync.preload(video)
        if not complete.wait(settings["capture_seconds"]):
            actual = sorted({parse_qs(urlsplit(row["url"]).query).get("tvid", [""])[0]
                             for row in captured.values() if urlsplit(row["url"]).path == settings["dash_path"]})
            raise RuntimeError(f"未捕获目标 TVID 的完整播放响应；实际请求编号：{actual}")
        if errors:
            raise RuntimeError(errors[0])
        Event().wait(settings["followup_seconds"])
    finally:
        try:
            if script is not None:
                try:
                    try:
                        removed = script.exports_sync.cancel()
                        if removed:
                            deadline = time.monotonic() + settings["cleanup_timeout_seconds"]
                            while script.exports_sync.pending():
                                if time.monotonic() >= deadline:
                                    raise RuntimeError("本次临时播放预读尚未清理完成")
                                Event().wait(settings["cleanup_poll_seconds"])
                    finally:
                        script.exports_sync.close()
                finally:
                    script.unload()
        finally:
            if session is not None:
                session.detach()
    if errors:
        raise RuntimeError(errors[0])
    return list(captured.values())


def replay(record: dict, profile: dict) -> dict:
    """在内存重放 record 的同域原生请求；返回有界 JSON，错误中不暴露地址或票据。"""
    settings = profile["playback"]
    if urlsplit(record["url"]).hostname != settings["dash_host"]:
        raise ValueError("只允许向原生 DASH 域名重放会话请求头")
    headers = {}
    for line in record["headers"]:
        name, separator, value = line.partition(":")
        if separator and name.casefold() not in ("host", "accept-encoding", "connection", "content-length"):
            headers[name.strip()] = value.strip()
    try:
        with open_network(profile, Request(record["url"], headers=headers), session_headers=True) as response:
            body = response.read(settings["max_body_bytes"] + 1)
        if len(body) > settings["max_body_bytes"]:
            raise ValueError("响应超过上限")
        return json.loads(body.decode("utf-8-sig"))
    except Exception:
        raise RuntimeError("播放会话重放未取得有效的有界 JSON 响应") from None


def response_summary(payload: dict) -> dict:
    """将 payload 的资源信息转换为报告白名单；返回无地址、票据或账号信息的结构。"""
    data = payload.get("data", {})
    program = data.get("program", {})
    result = {"code": payload.get("code"), "tvid": data.get("tvid"), "data_fields": sorted(data), "p_type": type(data.get("p")).__name__}
    for kind in ("video", "audio"):
        result[kind] = []
        for item in program.get(kind, []):
            safe = {key: item[key] for key in ("bid", "vid", "aid", "dr", "br", "fr", "code", "scrsz", "duration", "vsize", "fps", "_selected", "ct", "ff", "mver", "zqyh")
                    if key in item and isinstance(item[key], (str, int, float, bool, type(None)))}
            safe["fields"] = sorted(item)
            safe["resources"] = {key: {"type": type(item[key]).__name__, "length": len(item[key]) if isinstance(item[key], (list, dict, str)) else None}
                                 for key in ("fs", "ft", "flvs", "m3u8", "m3u8Url", "mpdUrl", "drm", "mu", "ml", "mk", "ms", "play", "p2p", "ps", "p") if key in item}
            for key, shape in safe["resources"].items():
                value = item[key]
                if isinstance(value, dict):
                    shape["fields"] = sorted(value)
                elif isinstance(value, list) and value and isinstance(value[0], dict):
                    shape["entry_fields"] = sorted(value[0])
            if isinstance(item.get("drm"), dict):
                safe["authorization"] = {key: item["drm"][key] for key in ("code", "preview", "licenseVersion", "drmVersion") if key in item["drm"]}
            result[kind].append(safe)
    if isinstance(data.get("p"), dict):
        result["p_fields"] = sorted(data["p"])
    return result


def fetch_manifest(profile: dict, source: dict) -> tuple[bytes, dict]:
    """读取 source 的内嵌清单或已声明地址；profile 限定域名及字节数，按内容识别格式并返回内存数据与安全摘要。"""
    inline = source.get("m3u8")
    if isinstance(inline, str) and inline.strip():
        body = inline.encode("utf-8")
        if len(body) > profile["playback"]["max_body_bytes"]:
            raise ValueError("内嵌媒体清单超过配置上限")
        summary = {"host": None, "bytes": len(body), "delivery": "inline"}
    else:
        url = source.get("ml") or source.get("m3u8Url")
        if not isinstance(url, str) or not url:
            raise RuntimeError("播放源既未提供内嵌清单，也未提供已适配的清单地址")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not parsed.hostname.endswith(profile["playback"]["resource_host_suffix"]):
            raise RuntimeError("清单地址不属于已核验的播放媒体域名")
        try:
            request = Request(url, headers={"User-Agent": profile["network"]["user_agent"]})
            with open_network(profile, request) as response:
                body = response.read(profile["playback"]["max_body_bytes"] + 1)
            if len(body) > profile["playback"]["max_body_bytes"]:
                raise ValueError("清单超过上限")
        except Exception:
            raise RuntimeError("获取播放媒体清单失败，临时地址未写入日志") from None
        summary = {"host": parsed.hostname, "bytes": len(body), "delivery": "url"}
    text = body.decode("utf-8-sig")
    if text.lstrip().startswith("#EXTM3U"):
        summary.update(format="m3u8", segments=sum(bool(line.strip()) and not line.startswith("#") for line in text.splitlines()),
                       tags=sorted({line.split(":", 1)[0] for line in text.splitlines() if line.startswith("#")}))
    elif text.lstrip().startswith("<"):
        root = ET.fromstring(text)
        summary.update(format="xml", root=root.tag, elements=[{"tag": item.tag, "attributes": sorted(item.attrib)} for item in list(root)[:20]])
    else:
        value = json.loads(text)
        summary.update(format="json", fields=sorted(value) if isinstance(value, dict) else [], type=type(value).__name__)
    return body, summary


def probe_playback(path: Path, index: int, sample: bool = False, full: bool = False) -> tuple[dict, Path]:
    """按 path 配置的画质研究清单第 index 项；sample 验证短段，full 验证并发布链接指定整集；返回安全报告及路径。"""
    profile = read_profile(path)
    links = linked_videos(profile)
    if index < 0 or index >= len(links):
        raise ValueError("index 超出当前链接清单范围")
    video = links[index]
    from .listing import task_options
    from .source import select_av_source, select_source
    if full:
        from .media import media_config
        source = select_av_source(profile, video, task_options(profile)[index]["quality"], media_config(profile)["audio"]["quality"])
    else:
        source = select_source(profile, video, task_options(profile)[index]["quality"])
    report = source.report
    output = profile["report_dir"] / (datetime.now().astimezone().strftime("session_%Y%m%d_%H%M%S_%f") + ".json")
    if sample:
        from .media import sample_media
        report["media_samples"] = sample_media(profile, source.selected, source.manifest, output.with_suffix(""), source.headers)
    if full:
        from .download import download_full
        report["full_media"] = download_full(profile, video, source.selected, source.manifest, output.with_suffix(""), source.headers,
                                              source.report["selection"])
        report["media_verified"] = report["full_media"]["media_verified"]
        report["highest_quality_verified"] = report["full_media"]["highest_quality_verified"]
        report["highest_audio_verified"] = report["full_media"]["highest_audio_verified"]
    atomic_json(output, report)
    return report, output
