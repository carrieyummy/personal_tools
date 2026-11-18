"""读取优酷本机配置、前端代码及缓存，输出不含会话值的研究证据。"""

from __future__ import annotations

import hashlib
import json
import ntpath
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

# UPS 已知公开流描述字段；配置只能选择此集合，不能借白名单导出 URL、签名或密钥。
PUBLIC_STREAM_FIELDS = frozenset({"stream_type", "width", "height", "size", "milliseconds_video",
                                  "milliseconds_audio", "drm_type", "media_type", "audio_lang", "subtitle_lang"})


def resolve_path(value: str, base: Path) -> Path:
    """展开输入路径；value 为含 Windows 环境变量的路径，base 为相对基准；返回绝对 Path。"""
    path = Path(ntpath.expandvars(value)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def load_config(path: Path) -> dict[str, Any]:
    """读取并校验 YAML；path 为配置路径；返回含绝对路径和样本信息的配置，错误抛 ValueError。"""
    path = path.expanduser().resolve()
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("研究配置必须是 YAML 对象")
    for name in ("client_dir", "user_data", "native_data", "download_list", "output_dir"):
        if not isinstance(config.get(name), str) or not config[name].strip():
            raise ValueError(f"缺少路径配置：{name}")
        config[name] = resolve_path(config[name], path.parent)
    if " " in str(config["output_dir"]):
        raise ValueError("研究输出路径不能包含空格")
    for name in ("context_chars", "matches_per_term"):
        if type(config["source"][name]) is not int or config["source"][name] <= 0:
            raise ValueError(f"source.{name} 必须为正整数")
    media = config["media"]
    restoration = config["restoration"]
    for name in ("function_rva", "output_bytes", "timeout_seconds"):
        if type(restoration[name]) is not int or restoration[name] <= 0:
            raise ValueError(f"restoration.{name} 必须为正整数")
    for name in ("library_sha256", "challenge_module_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", restoration[name]):
            raise ValueError(f"restoration.{name} 必须为 SHA-256")
    if not 25 <= restoration["output_bytes"] <= 4096:
        raise ValueError("restoration.output_bytes 超出已支持的缓冲区范围")
    if Path(restoration["library"]).is_absolute() or ".." in Path(restoration["library"]).parts:
        raise ValueError("restoration.library 必须位于客户端安装目录内")
    for name in ("work_dir", "ffmpeg"):
        media[name] = resolve_path(media[name], path.parent)
    for name in ("timeout_seconds", "max_manifest_bytes", "max_segment_bytes", "decode_timeout_seconds"):
        if type(media[name]) is not int or media[name] <= 0:
            raise ValueError(f"media.{name} 必须为正整数")
    positions = media["sample_positions"]
    if not isinstance(positions, list) or not positions or any(type(x) not in (float, int) or not 0 <= x <= 1 for x in positions):
        raise ValueError("media.sample_positions 必须为 0 到 1 的非空位置列表")
    domains = media["allowed_domains"]
    if not isinstance(domains, list) or not domains or any(not isinstance(x, str) or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,}", x) for x in domains):
        raise ValueError("media.allowed_domains 必须为有效域名后缀列表")
    if not isinstance(media["allowed_schemes"], list) or not media["allowed_schemes"] or any(x not in ("http", "https") for x in media["allowed_schemes"]):
        raise ValueError("media.allowed_schemes 仅允许 http/https")
    if not isinstance(media["stream_type"], str) or not media["stream_type"] or not isinstance(media["user_agent"], str) or not media["user_agent"]:
        raise ValueError("media.stream_type 和 user_agent 必须为非空字符串")
    for name in ("max_file_bytes", "max_total_bytes", "max_json_candidates_per_file"):
        if type(config["cache"][name]) is not int or config["cache"][name] <= 0:
            raise ValueError(f"cache.{name} 必须为正整数")
    for section, names in (("native", ("timeout_seconds",)),
                           ("network", ("timeout_seconds", "max_response_bytes")),
                           ("playback_log", ("max_file_bytes", "max_total_bytes", "decode_rounds")),
                           ("live", ("timeout_seconds", "max_result_bytes", "connect_attempts", "close_delay_ms"))):
        for name in names:
            if type(config[section][name]) is not int or config[section][name] <= 0:
                raise ValueError(f"{section}.{name} 必须为正整数")
    endpoint = urlsplit(config["live"]["inspector_url"])
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port:
        raise ValueError("live.inspector_url 必须为明确端口的 127.0.0.1 HTTP 地址")
    interval = config["live"]["retry_interval_seconds"]
    if type(interval) not in (float, int) or interval <= 0:
        raise ValueError("live.retry_interval_seconds 必须为正数")
    for case in config["live"]["cases"]:
        for key in ("remove_biz_fields", "ad_fields"):
            value = case[key]
            if key == "ad_fields" and value is None:
                continue
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"live.cases.{key} 必须为字段名列表")
    for case in config["network"]["ups"]["cases"]:
        if case["user_agent_source"] not in ("template", "client_log"):
            raise ValueError("user_agent_source 必须为 template 或 client_log")
        if case["device_source"] not in ("cookie", "client_log"):
            raise ValueError("device_source 必须为 cookie 或 client_log")
        if type(case["client_hardware_fields"]) is not bool:
            raise ValueError("client_hardware_fields 必须为布尔值")
    fields = config["cache"]["stream_fields"]
    if not isinstance(fields, list) or not fields or any(k not in PUBLIC_STREAM_FIELDS for k in fields):
        raise ValueError("cache.stream_fields 只能选择公开流元数据，禁止 URL、会话和密钥字段")
    config["cache"]["roots"] = [resolve_path(p, path.parent) for p in config["cache"]["roots"]]
    tasks = yaml.safe_load(config["download_list"].read_text(encoding="utf-8"))
    items = tasks.get("download_list") if isinstance(tasks, dict) else None
    index = config["sample_index"]
    if not isinstance(items, list) or type(index) is not int or not 0 <= index < len(items):
        raise ValueError("sample_index 必须是下载清单的有效下标")
    if not isinstance(items[index], str) or not items[index].strip() or items[index] != items[index].strip():
        raise ValueError("研究样本必须是非空链接字符串")
    config["sample"] = {"url": items[index]}
    return config


def write_report(config: dict[str, Any], name: str, data: Any) -> Path:
    """写入脱敏 JSON；config 为已校验配置，name 为报告名，data 为安全数据；返回输出路径。"""
    target = config["output_dir"] / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def snapshot(config: dict[str, Any]) -> dict[str, Any]:
    """采集本机环境；config 为研究配置；返回版本、路径、会话存在性及下载数量，不读取 Cookie 值。"""
    user = config["user_data"]
    data = json.loads((user / "data.json").read_text(encoding="utf-8"))
    package = config["client_dir"] / "resources/app/package.json"
    info = json.loads(package.read_text(encoding="utf-8"))
    download = data.get("download", {})
    result = {
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "sample": config["sample"],
        "client": {k: info.get(k) for k in ("name", "version", "buildnumber")},
        "client_dir": str(config["client_dir"]),
        "session": {"cookies_file_exists": (user / "Cookies").is_file(),
                    "login_sync_status": data.get("loginSyncStatus"),
                    "note": "存在性不能证明当前账号权益；须结合客户端页面和接口实测。"},
        "download_path": data.get("downloadPath"),
        "acc_cache_dir": data.get("accCacheDir"),
        "local_media_server_port": data.get("serverPort"),
        "download_task_count": len(download.get("taskList", [])),
        "download_config_count": len(download.get("configs", {})),
        "cookie_inventory": [],
    }
    try:
        # 只查域名、名称和存储长度；禁止 SELECT value/encrypted_value 或解密登录凭据。
        with closing(sqlite3.connect((user / "Cookies").as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            rows = db.execute("SELECT host_key, name, length(encrypted_value), length(value) "
                              "FROM cookies WHERE host_key LIKE '%youku.com'").fetchall()
            result["cookie_inventory"] = [dict(zip(("domain", "name", "encrypted_bytes", "plain_length"), r))
                                          for r in rows]
    except sqlite3.Error as error:
        result["cookie_read_error"] = type(error).__name__
    return result


def source_evidence(config: dict[str, Any]) -> dict[str, Any]:
    """定位静态代码证据；config 含文件 glob、搜索词和截取范围；返回哈希、偏移及有限代码片段。"""
    options = config["source"]
    roots = [(config["client_dir"] / "resources", options["globs"]),
             (config["user_data"], options["cache_globs"])]
    files: set[Path] = set()
    for root, globs in roots:
        for pattern in globs:
            files.update(root.glob(pattern))
    evidence = []
    for path in sorted(files):
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        matches = []
        for term in options["terms"]:
            for match in list(re.finditer(re.escape(term), text))[:options["matches_per_term"]]:
                start = max(0, match.start() - options["context_chars"])
                end = match.end() + options["context_chars"]
                matches.append({"term": term, "offset": match.start(), "snippet": text[start:end]})
        if matches:
            evidence.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                             "matches": matches})
    return {"files": evidence, "note": "静态代码仅证明实现分支存在，不证明服务器向本账号开放对应档位。"}


def stream_summary(document: Any, fields: list[str], json_path: str = "$") -> list[dict[str, Any]]:
    """递归提取流元数据；document 为 JSON，fields 为公开标量字段，json_path 为节点定位；返回带来源的脱敏列表。"""
    streams = []
    if isinstance(document, dict):
        value = document.get("stream")
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                row = {k: item[k] for k in fields if k in PUBLIC_STREAM_FIELDS and k in item
                       and isinstance(item[k], (str, int, float, bool))}
                if row:
                    row["json_path"] = json_path + ".stream"
                    row["segment_count"] = len(item.get("segs", [])) if isinstance(item.get("segs"), list) else 0
                    row["has_m3u8_url"] = bool(item.get("m3u8_url"))
                    row["has_mpd_url"] = bool(item.get("mpd_url"))
                    extra = item.get("stream_ext")
                    if isinstance(extra, dict):
                        row["stream_ext_fields"] = sorted(extra)
                        row["features"] = {k: extra[k] for k in ("feature_show", "feature_list", "stream_desc", "format")
                                           if k in extra and (isinstance(extra[k], (str, int, float, bool))
                                                              or isinstance(extra[k], list)
                                                              and all(isinstance(v, str) for v in extra[k]))}
                    streams.append(row)
        for key, item in document.items():
            if key != "stream":
                streams.extend(stream_summary(item, fields, json_path + "." + str(key)))
    elif isinstance(document, list):
        for index, item in enumerate(document):
            streams.extend(stream_summary(item, fields, f"{json_path}[{index}]"))
    return streams


def scan_cache(config: dict[str, Any]) -> dict[str, Any]:
    """只读搜索已缓存的 UPS JSON；config 提供扫描预算和字段白名单；返回流摘要、跳过和受限信息。"""
    options = config["cache"]
    result: dict[str, Any] = {"read_bytes": 0, "read_files": 0, "locked_files": [], "oversized_files": [],
                              "stream_documents": [], "budget_exhausted": False}
    decoder = json.JSONDecoder()
    for root in options["roots"]:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size > options["max_file_bytes"]:
                result["oversized_files"].append(str(path))
                continue
            if result["read_bytes"] + size > options["max_total_bytes"]:
                result["budget_exhausted"] = True
                return result
            try:
                raw = path.read_bytes()
            except OSError as error:
                result["locked_files"].append({"path": str(path), "error": type(error).__name__})
                continue
            result["read_bytes"] += len(raw)
            result["read_files"] += 1
            text = raw.decode("utf-8", errors="replace")
            positions = {0}
            for marker in options["json_markers"]:
                positions.update(m.start() for m in re.finditer(re.escape(marker), text))
            for start in sorted(positions)[:options["max_json_candidates_per_file"]]:
                try:
                    doc, _ = decoder.raw_decode(text, start)
                except ValueError:
                    continue
                streams = stream_summary(doc, options["stream_fields"])
                if streams:
                    result["stream_documents"].append({"path": str(path), "offset": start,
                                                       "streams": streams,
                                                       "attribution": "缓存可能含历史响应；须另核对剧集和会话时间。"})
                    break
    return result


def analyze_ups(config: dict[str, Any], path: Path) -> dict[str, Any]:
    """分析已取得的 JSON 响应；config 为字段白名单，path 为本地 JSON；仅返回流元数据，不进行联网。"""
    data = json.loads(path.expanduser().read_text(encoding="utf-8-sig"))
    streams = stream_summary(data, config["cache"]["stream_fields"])
    # 像素数仅用于排列，不把未经确认的 DRM 流标记为可独立下载。
    streams.sort(key=lambda s: (int(s.get("width") or 0) * int(s.get("height") or 0),
                                int(s.get("size") or 0)), reverse=True)
    return {"streams": streams, "independent_file_verified": False,
            "note": "需继续验证媒体保护、完整音视频和全片解码；列出高分辨率不等于取得可独立播放文件。"}
