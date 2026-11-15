"""研究客户端登录态和播放接口；输出脱敏报告，不操作界面或保存登录凭据。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
from string import Template
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
import uuid
import xml.etree.ElementTree as ET

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from local.client import Transport, load_user, normalize_url, source_request


def load_config(path: Path) -> dict[str, Any]:
    """读取专项 YAML；path 为配置文件路径；返回配置字典。"""
    config = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("研究配置必须是 YAML 映射。")
    return config


def resolve_path(value: str, base: Path = PROJECT_ROOT) -> Path:
    """展开路径；value 支持环境变量和 ~，base 为相对路径基准；返回绝对路径。"""
    path = Path(Template(value).substitute(os.environ)).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def read_user_data(path: Path) -> dict[str, Any]:
    """在内存解析客户端 Base64 用户数据；path 为本机文件；返回用户字典，不落盘。"""
    return load_user(path)


def describe_fields(value: Any, prefix: str = "") -> dict[str, str]:
    """描述数据结构；value 为待检查值，prefix 为字段前缀；仅返回字段名和类型。"""
    result: dict[str, str] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            result[name] = type(child).__name__
            if isinstance(child, dict):
                result.update(describe_fields(child, name))
    return result


def endpoint_shape(value: str) -> dict[str, Any]:
    """隐藏地址中的参数和长路径；value 为 URL；返回用于报告的地址结构。"""
    url = urlsplit(value.rstrip(",;)]}"))
    return {
        "host": url.hostname,
        "path": "/".join(part if len(part) < 36 else "[long]" for part in url.path.split("/")),
        "query_keys": sorted(parse_qs(url.query, keep_blank_values=True)),
    }


def pe_exports(raw: bytes) -> list[str]:
    """读取 PE 导出名；raw 为完整 DLL 字节；返回符号名，不加载或执行 DLL。"""
    pe_start = struct.unpack_from("<I", raw, 0x3C)[0]
    section_count = struct.unpack_from("<H", raw, pe_start + 6)[0]
    optional_size = struct.unpack_from("<H", raw, pe_start + 20)[0]
    optional_start = pe_start + 24
    magic = struct.unpack_from("<H", raw, optional_start)[0]
    directory_offset = 96 if magic == 0x10B else 112
    export_rva = struct.unpack_from("<I", raw, optional_start + directory_offset)[0]
    sections = []
    for index in range(section_count):
        start = optional_start + optional_size + index * 40
        size, address, raw_size, offset = struct.unpack_from("<IIII", raw, start + 8)
        sections.append((address, max(size, raw_size), offset))

    def offset_for(rva: int) -> int:
        """定位 PE 数据；rva 为映射地址；返回文件偏移，越界时抛出异常。"""
        for address, size, offset in sections:
            if address <= rva < address + size:
                return offset + rva - address
        raise ValueError("PE 地址不在已知节中。")

    if not export_rva:
        return []
    export_start = offset_for(export_rva)
    count = struct.unpack_from("<I", raw, export_start + 24)[0]
    names_start = offset_for(struct.unpack_from("<I", raw, export_start + 32)[0])
    names = []
    for index in range(count):
        name_rva = struct.unpack_from("<I", raw, names_start + index * 4)[0]
        start = offset_for(name_rva)
        end = raw.index(b"\0", start)
        names.append(raw[start:end].decode("ascii", errors="replace"))
    return names


def inspect_client(config: dict[str, Any]) -> dict[str, Any]:
    """检查本机客户端结构；config 为专项配置；返回不含字段值和凭据的检查结果。"""
    paths = config["paths"]
    data_root = resolve_path(paths["client_data"])
    install_root = resolve_path(paths["client_install"])
    user_data = read_user_data(resolve_path(paths["user_data"], data_root))
    report: dict[str, Any] = {"user_fields": describe_fields(user_data), "logs": {}, "binaries": {}}
    global_root = ET.fromstring(resolve_path(paths["global_data"], data_root).read_bytes())
    report["global_xml_schema"] = sorted({(node.tag, tuple(node.attrib)) for node in global_root.iter()})
    report["global_userinfo_locations"] = [
        {"tag": node.tag, "matching_attribute": key, "attribute_names": list(node.attrib), "text_length": len(node.text or "")}
        for node in global_root.iter() for key, value in node.attrib.items() if value == "key_userinfo"
    ]
    values = {key: value for node in global_root.iter() for key, value in node.attrib.items()}
    global_user_value = values.get(paths["global_user_key"])
    if global_user_value:
        global_user = json.loads(global_user_value)
        report["global_user_fields"] = describe_fields(global_user)
        report["user_ticket_matches_global"] = global_user.get("ticket") == user_data.get("ticket")
    report["cached_device_configs"] = []
    for key, value in values.items():
        if not key.startswith(paths["device_config_prefix"]):
            continue
        summary = {"value_length": len(value), "header_hex": value.encode()[:16].hex()}
        if value.lstrip().startswith("{"):
            summary["field_types"] = describe_fields(json.loads(value))
        else:
            summary["format"] = "not_json"
        report["cached_device_configs"].append(summary)
    for name in config["inspection"]["log_names"]:
        path = data_root / "logs" / name
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        endpoints: list[dict[str, Any]] = []
        seen: set[str] = set()
        for match in re.finditer(r"https?://[^\s\x22\x27<>\\]+", text):
            try:
                shape = endpoint_shape(match.group())
            except ValueError:
                continue
            host = shape["host"] or ""
            if not (host.endswith((".api.mgtv.com", ".glb.mgtv.com")) or host.startswith("tinker.")):
                continue
            key = json.dumps(shape, sort_keys=True)
            if key not in seen:
                seen.add(key)
                endpoints.append(shape)
            if len(endpoints) >= config["inspection"]["max_endpoints_per_log"]:
                break
        functions = sorted(set(re.findall(r"\[([A-Za-z_][A-Za-z0-9_:~.-]*:\d+)\]", text)))
        report["logs"][name] = {
            "bytes": len(raw),
            "header_hex": raw[:24].hex(),
            "endpoints": endpoints,
            "playback_functions": [name for name in functions if re.search("[Ll]ayer|[Ss]ource|[Pp]layerData|[Aa]uth|[Tt]icket", name)],
        }
    for name in config["inspection"]["binaries"]:
        path = install_root / name
        raw = path.read_bytes()
        endpoints = []
        for value in re.findall(rb"https?://[\x21-\x7e]{3,200}", raw):
            text = value.decode("ascii", errors="replace")
            if not any(term in text.lower() for term in ("mgtv", "hitv", "imgo")):
                continue
            try:
                endpoints.append(endpoint_shape(text))
            except ValueError:
                continue
        strings = "\n".join(value.decode("ascii") for value in re.findall(rb"[\x20-\x7e]{4,}", raw))
        terms = sorted({match.group() for pattern in config["inspection"]["binary_string_patterns"] for match in re.finditer(pattern, strings)})
        report["binaries"][name] = {
            "bytes": len(raw), "endpoints": endpoints,
            "api_terms": terms,
            "exports": pe_exports(raw),
            "contexts": {},
        }
        for anchor in config["inspection"]["context_anchors"]:
            position = raw.find(anchor.encode("ascii"))
            if position < 0:
                continue
            radius = config["inspection"]["context_bytes"]
            window = raw[max(0, position - radius):position + radius]
            report["binaries"][name]["contexts"][anchor] = [
                value.decode("ascii") for value in re.findall(rb"[\x20-\x7e]{4,}", window)
            ]
    return report


def save_report(report: dict[str, Any], config: dict[str, Any], name: str) -> Path:
    """保存脱敏 JSON；report 为报告，config 指定目录，name 为文件名；返回文件路径。"""
    directory = resolve_path(config["paths"]["report_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def request_json(endpoint: str, query: dict[str, Any], config: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """请求研究接口；endpoint 为已配置地址，query 为内存参数，config 控制网络；返回数据和脱敏摘要。"""
    request = Request(endpoint + "?" + urlencode(query), headers=config["network"]["headers"])
    summary: dict[str, Any] = {"endpoint": endpoint_shape(endpoint)}
    try:
        response = urlopen(request, timeout=config["network"]["timeout_seconds"])
    except HTTPError as error:
        response = error
    except URLError as error:
        return None, {**summary, "transport_error": type(error.reason).__name__}
    with response:
        raw = response.read(config["network"]["max_response_bytes"] + 1)
        summary.update(http_status=response.code, bytes=len(raw))
    if len(raw) > config["network"]["max_response_bytes"]:
        return None, {**summary, "error": "response_limit_exceeded"}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return None, {**summary, "error": "not_json"}
    if isinstance(data, dict):
        summary["top_fields"] = list(data)
        summary["status_fields"] = {key: value for key, value in data.items() if key in ("code", "status", "err_code") and isinstance(value, (str, int, bool))}
        summary["message"] = {
            key: re.sub(r"[A-Za-z0-9_+/=-]{20,}", "[redacted]", str(data[key]))[:200]
            for key in ("msg", "message", "err_msg") if key in data
        }
        if isinstance(data.get("data"), dict):
            summary["data_fields"] = list(data["data"])
            payload = data["data"]
            if isinstance(payload.get("user"), dict):
                summary["user_fields"] = describe_fields(payload["user"])
                summary["user_status"] = {key: payload["user"][key] for key in ("isvip", "isVip", "vip", "login", "isLogin", "userType") if key in payload["user"] and isinstance(payload["user"][key], (int, bool))}
    return data, summary


def media_nodes(value: Any) -> list[dict[str, Any]]:
    """遍历媒体响应；value 为任意接口数据；返回带地址及码流信息的节点，仅用于内存处理。"""
    result = []
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("url") and any(key in node for key in ("definition", "standardName", "barName", "filebitrate")):
                result.append(node)
            stack.extend(child for child in node.values() if isinstance(child, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)
    return result


def stream_summaries(value: Any) -> list[dict[str, Any]]:
    """提取画质摘要；value 为任意接口数据；返回不包含地址和签名的码流列表。"""
    result = []
    fields = ("definition", "name", "standardName", "barName", "videoWidth", "videoHeight", "videoFormat", "filebitrate", "isHdr", "needPay")
    for node in media_nodes(value):
        summary = {key: node[key] for key in fields if key in node and isinstance(node[key], (str, int, float, bool))}
        if "disp" in node:
            summary["dispatch_type"] = type(node["disp"]).__name__
            if isinstance(node["disp"], dict):
                summary["dispatch_fields"] = list(node["disp"])
        result.append(summary)
    return result


def native_request(target: str, user: dict[str, Any], config: dict[str, Any]) -> tuple[str, Any, dict[str, Any]]:
    """请求单集原生播放信息；target 为分享链接，user 为本机登录态，config 为协议参数；返回编号、数据和摘要。"""
    _, clip_id, video_id = normalize_url(target)
    profile = {"endpoint": config["native_api"]["source_endpoint"], "query": config["native_api"]["query"], "exdef": config["native_api"]["exdef"]}
    network = {**config["network"], "max_json_bytes": config["network"]["max_response_bytes"]}
    data, summary = source_request(profile, user, clip_id, video_id, Transport(network, []))
    return video_id, data, summary


def fetch_bytes(url: str, config: dict[str, Any], limit: int) -> tuple[bytes | None, dict[str, Any]]:
    """读取已授权媒体资源；url 仅驻留内存，config 指定请求策略，limit 限制字节数；返回内容和脱敏状态。"""
    if urlsplit(url).scheme not in ("http", "https"):
        return None, {"error": "unsupported_media_url_scheme"}
    request = Request(url, headers=config["network"]["headers"])
    try:
        with urlopen(request, timeout=config["network"]["timeout_seconds"]) as response:
            raw = response.read(limit + 1)
            summary = {"http_status": response.status, "bytes": len(raw)}
    except HTTPError as error:
        return None, {"http_status": error.code}
    except URLError as error:
        return None, {"transport_error": type(error.reason).__name__}
    if len(raw) > limit:
        return None, {**summary, "error": "response_limit_exceeded"}
    return raw, summary


def resolve_playback(config: dict[str, Any]) -> dict[str, Any]:
    """验证分享链接到完整芒臻清单和抽样分片；config 提供样本及验收参数；返回不含凭据的验收报告。"""
    data_root = resolve_path(config["paths"]["client_data"])
    user = read_user_data(resolve_path(config["paths"]["user_data"], data_root))
    report: dict[str, Any] = {"ui_automation": False, "credential_source": config["paths"]["user_data"], "results": []}
    for target in config["targets"]:
        video_id, data, response = native_request(target, user, config)
        result: dict[str, Any] = {"video_id": video_id, "api": response, "passed": False}
        report["results"].append(result)
        if not isinstance(data, dict) or data.get("code") != 200:
            result["error"] = "playback_not_authorized_or_unavailable"
            continue
        streams = [node for node in media_nodes(data) if node.get("standardName") == config["selection"]["quality_name"] and node.get("videoWidth")]
        if not streams:
            result["error"] = "requested_quality_missing"
            continue
        stream = max(streams, key=lambda node: float(node.get("filebitrate", 0)))
        result["stream"] = stream_summaries(stream)[0]
        dispatch = stream.get("disp")
        if not isinstance(dispatch, dict) or not isinstance(dispatch.get("info"), str):
            result["error"] = "embedded_dispatch_missing"
            continue
        manifest_url = dispatch["info"]
        raw, manifest_state = fetch_bytes(manifest_url, config, config["network"]["max_response_bytes"])
        result["manifest"] = manifest_state
        if raw is None:
            continue
        manifest = raw.decode("utf-8-sig", errors="strict")
        if not manifest.startswith("#EXTM3U"):
            result["error"] = "not_hls_manifest"
            continue
        lines = manifest.splitlines()
        entries = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
        durations = [float(value) for value in re.findall(r"^#EXTINF:([\d.]+)", manifest, re.M)]
        methods = re.findall(r"^#EXT-X-KEY:.*?METHOD=([^,\r\n]+)", manifest, re.M)
        total_duration = sum(durations)
        result["manifest"].update(segments=len(entries), duration_seconds=round(total_duration, 6), endlist="#EXT-X-ENDLIST" in manifest, encryption_methods=methods)
        if not entries or len(entries) != len(durations) or "#EXT-X-ENDLIST" not in manifest or any(method != "NONE" for method in methods):
            result["error"] = "manifest_not_complete_clear_media_playlist"
            continue
        expected_duration = float(stream["ftime"])
        result["manifest"]["api_duration_seconds"] = expected_duration
        if abs(total_duration - expected_duration) > config["verification"]["duration_tolerance_seconds"]:
            result["error"] = "manifest_duration_mismatch"
            continue
        directory = resolve_path(config["paths"]["report_dir"]) / "media" / video_id
        directory.mkdir(parents=True, exist_ok=True)
        samples = []
        for fraction in config["verification"]["sample_positions"]:
            if not 0 <= fraction <= 1:
                raise ValueError("抽样位置必须在 0 到 1 之间。")
            index = round(fraction * (len(entries) - 1))
            content, sample = fetch_bytes(urljoin(manifest_url, entries[index]), config, config["verification"]["sample_limit_bytes"])
            sample["index"] = index
            if content is not None:
                path = directory / f"segment_{index:06d}.ts"
                path.write_bytes(content)
                sample["file"] = path.relative_to(PROJECT_ROOT).as_posix()
                command = [str(resolve_path(config["verification"]["ffmpeg_path"])), "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-f", "null", "NUL"]
                decoded = subprocess.run(command, capture_output=True, timeout=config["verification"]["decode_timeout_seconds"], check=False)
                sample["decode_exit_code"] = decoded.returncode
                sample["decode_error"] = decoded.stderr.decode("utf-8", errors="replace")[:2000]
            samples.append(sample)
        result["samples"] = samples
        result["passed"] = bool(samples) and all(sample.get("decode_exit_code") == 0 for sample in samples)
    report["passed"] = bool(report["results"]) and all(item["passed"] for item in report["results"])
    return report


def probe_catalog(config: dict[str, Any]) -> dict[str, Any]:
    """读取目标剧集的公开选集元数据；config 提供目录接口和首个链接；返回集数编号及状态。"""
    target = config["targets"][0]
    match = re.search(r"/b/(\d+)/", urlsplit(target).path)
    if not match:
        raise ValueError("目标链接缺少剧集编号。")
    context = {"clip_id": match.group(1), "app_version": config["native_api"]["query"]["appVersion"]}
    query = {key: str(value).format_map(context) for key, value in config["catalog_api"]["query"].items()}
    data, summary = request_json(config["catalog_api"]["endpoint"], query, config)
    videos = []
    seen = set()
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            video_id = node.get("videoId")
            if video_id and video_id not in seen:
                seen.add(video_id)
                videos.append({key: node[key] for key in ("videoId", "videoIndex", "sname", "nname", "name", "allowDownload", "status", "isVip") if key in node and isinstance(node[key], (str, int, bool))})
            stack.extend(child for child in node.values() if isinstance(child, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)
    return {"clip_id": context["clip_id"], "response": summary, "videos": videos}


def inspect_native_xrefs(config: dict[str, Any]) -> dict[str, Any]:
    """静态定位原生请求构造代码；config 指定 DLL 和范围；返回反汇编与常量，不执行 DLL。"""
    from capstone import Cs, CS_ARCH_X86, CS_MODE_32

    settings = config["disassembly"]
    binary = resolve_path(settings["binary"], resolve_path(config["paths"]["client_install"]))
    raw = binary.read_bytes()
    pe_start = struct.unpack_from("<I", raw, 0x3C)[0]
    section_count = struct.unpack_from("<H", raw, pe_start + 6)[0]
    optional_size = struct.unpack_from("<H", raw, pe_start + 20)[0]
    optional_start = pe_start + 24
    if struct.unpack_from("<H", raw, optional_start)[0] != 0x10B:
        raise ValueError("当前静态检查仅适用于已验证的 PE32 客户端模块。")
    image_base = struct.unpack_from("<I", raw, optional_start + 28)[0]
    sections = []
    for index in range(section_count):
        start = optional_start + optional_size + index * 40
        size, address, raw_size, offset = struct.unpack_from("<IIII", raw, start + 8)
        flags = struct.unpack_from("<I", raw, start + 36)[0]
        sections.append((address, max(size, raw_size), offset, raw_size, flags))

    def address_at(offset: int) -> int:
        """映射文件位置；offset 为 DLL 文件偏移；返回静态虚拟地址，未映射时抛错。"""
        for address, _, position, raw_size, _ in sections:
            if position <= offset < position + raw_size:
                return image_base + address + offset - position
        raise ValueError("文件位置没有映射到 PE 节。")

    def offset_at(address: int) -> int | None:
        """映射静态地址；address 为完整虚拟地址；返回文件位置，非本模块地址返回 None。"""
        rva = address - image_base
        for base, size, position, _, _ in sections:
            if base <= rva < base + size:
                return position + rva - base
        return None

    def string_at(address: int) -> str:
        """识别指针常量；address 为候选地址；返回可打印 ASCII/UTF-16 文本，否则为空。"""
        start = offset_at(address)
        if start is None or start >= len(raw):
            return ""
        value = raw[start:start + 240]
        match = re.match(rb"(?:[\x20-\x7e]\x00){2,}", value)
        if match:
            return match.group().decode("utf-16le")
        match = re.match(rb"[\x20-\x7e]{1,}", value)
        return match.group().decode("ascii") if match else ""

    imports: dict[int, str] = {}
    import_rva = struct.unpack_from("<I", raw, optional_start + 104)[0]
    descriptor = offset_at(image_base + import_rva) if import_rva else None
    while descriptor is not None:
        original, _, _, name_rva, first = struct.unpack_from("<IIIII", raw, descriptor)
        if not any((original, name_rva, first)):
            break
        thunk_offset = offset_at(image_base + (original or first))
        if thunk_offset is not None:
            index = 0
            while (thunk := struct.unpack_from("<I", raw, thunk_offset + index * 4)[0]):
                if not thunk & 0x80000000:
                    imports[image_base + first + index * 4] = string_at(image_base + thunk + 2)
                index += 1
        descriptor += 20
    engine = Cs(CS_ARCH_X86, CS_MODE_32)
    report: dict[str, Any] = {"binary": settings["binary"], "sha256": hashlib.sha256(raw).hexdigest(), "references": []}
    references = []
    export_rva = struct.unpack_from("<I", raw, optional_start + 96)[0]
    if export_rva and settings["symbols"]:
        export_start = offset_at(image_base + export_rva)
        count = struct.unpack_from("<I", raw, export_start + 24)[0]
        function_rva, names_rva, ordinals_rva = struct.unpack_from("<III", raw, export_start + 28)
        functions = offset_at(image_base + function_rva)
        names = offset_at(image_base + names_rva)
        ordinals = offset_at(image_base + ordinals_rva)
        for index in range(count):
            name_rva = struct.unpack_from("<I", raw, names + index * 4)[0]
            name = string_at(image_base + name_rva)
            if not any(re.search(pattern, name) for pattern in settings["symbols"]):
                continue
            ordinal = struct.unpack_from("<H", raw, ordinals + index * 2)[0]
            rva = struct.unpack_from("<I", raw, functions + ordinal * 4)[0]
            start = offset_at(image_base + rva)
            references.append((name, start, start))
    for anchor in settings["anchors"]:
        anchor_offset = raw.find(anchor.encode("ascii"))
        if anchor_offset < 0:
            continue
        anchor_address = address_at(anchor_offset)
        for match in re.finditer(re.escape(struct.pack("<I", anchor_address)), raw):
            reference = match.start()
            if not any(position <= reference < position + raw_size and flags & 0x20 for _, _, position, raw_size, flags in sections):
                continue
            limit = max(0, reference - settings["bytes_before"])
            prologue = raw.rfind(b"\x55\x8b\xec", limit, reference)
            start = prologue if prologue >= limit else limit
            references.append((anchor, reference, start))
            if len(references) >= settings["max_references"]:
                break
    for anchor, reference, start in references:
        instructions = []
        for instruction in engine.disasm(raw[start:reference + settings["bytes_after"]], address_at(start)):
            hints = []
            for value in re.findall(r"0x[0-9a-f]+", instruction.op_str):
                address = int(value, 16)
                target_offset = offset_at(address)
                thunk_name = ""
                if target_offset is not None and raw[target_offset:target_offset + 2] == b"\xff\x25":
                    thunk_name = imports.get(struct.unpack_from("<I", raw, target_offset + 2)[0], "")
                text = imports.get(address) or thunk_name
                if not text and instruction.mnemonic not in ("call", "jmp"):
                    text = string_at(address)
                if text:
                    hints.append(text)
            instructions.append(f"{instruction.address:08x}: {instruction.mnemonic} {instruction.op_str}" + (" ; " + " | ".join(hints) if hints else ""))
        report["references"].append({"anchor": anchor, "offset": reference, "instructions": instructions})
    report["additional_binaries"] = []
    for job in settings.get("additional_jobs", []):
        nested = {**config, "disassembly": {**settings, **job, "additional_jobs": []}}
        report["additional_binaries"].append(inspect_native_xrefs(nested))
    return report


def probe_apis(config: dict[str, Any]) -> dict[str, Any]:
    """复用登录态比较网页和原生播放接口；config 提供样本及参数；返回脱敏实测报告。"""
    data_root = resolve_path(config["paths"]["client_data"])
    user = read_user_data(resolve_path(config["paths"]["user_data"], data_root))
    context = {"user_uuid": user["uuid"], "ticket": user["ticket"]}
    auth_query = {key: str(value).format_map(context) for key, value in config["auth_probe"]["query"].items()}
    auth = {"enabled": config["auth_probe"]["enabled"]}
    if config["auth_probe"]["enabled"]:
        _, auth = request_json(config["auth_probe"]["endpoint"], auth_query, config)
    report: dict[str, Any] = {"ui_automation": False, "local_vip_status": bool(user.get("vipinfo", {}).get("isvip")), "ticket_check": auth, "results": []}
    for target in config["targets"]:
        video_id, data, summary = native_request(target, user, config)
        report["results"].append({"video_id": video_id, "route": "native_ticket", "response": summary, "streams": stream_summaries(data)})
        web = config["web_api"]
        if not web["enabled"]:
            continue
        device_id = str(uuid.uuid4())
        tk2 = base64.urlsafe_b64encode(f"did={device_id}|pno={web['client_number']}|ver={web['client_version']}|clit={int(time.time())}".encode())[::-1].decode()
        web_query = {"tk2": tk2, "video_id": video_id, "type": web["video_type"], "uuid": user["uuid"], "ticket": user["ticket"]}
        data, summary = request_json(web["video_endpoint"], web_query, config)
        report["results"].append({"video_id": video_id, "route": "web_video_ticket", "response": summary, "streams": stream_summaries(data)})
        payload = data.get("data", {}) if isinstance(data, dict) else {}
        atc = payload.get("atc", {}) if isinstance(payload, dict) else {}
        if isinstance(atc, dict) and atc.get("pm2"):
            source_query = {**web_query, **web["source_query"], "pm2": atc["pm2"]}
            source, source_summary = request_json(web["source_endpoint"], source_query, config)
            report["results"].append({"video_id": video_id, "route": "web_source_ticket", "response": source_summary, "streams": stream_summaries(source)})
    return report


def query_native_caps(config: dict[str, Any]) -> dict[str, Any]:
    """通过独立 32 位进程查询真实播放器能力；config 指定已验证 DLL；返回能力与版本指纹。"""
    settings = config["native_caps"]
    dll = resolve_path(settings["dll"], resolve_path(config["paths"]["client_install"]))
    digest = hashlib.sha256(dll.read_bytes()).hexdigest()
    if digest != settings["expected_sha256"]:
        raise ValueError("播放器 DLL 已变化，需重新核对能力查询接口。")
    request = {**settings, "dll_path": str(dll)}
    request_path = save_report(request, config, "native_caps_request.json")
    command = [str(resolve_path(settings["host"])), "-NoProfile", "-NonInteractive", "-File", str(resolve_path(settings["helper"])), "-RequestPath", str(request_path)]
    result = subprocess.run(command, capture_output=True, timeout=settings["timeout_seconds"], check=False)
    if result.returncode:
        return {"exit_code": result.returncode, "stderr": result.stderr.decode("utf-8", errors="replace"), "sha256": digest}
    output = result.stdout.decode("utf-8-sig", errors="replace").strip()
    marker = settings["output_marker"]
    if marker not in output:
        return {"exit_code": 1, "error": "missing_result_marker", "sha256": digest}
    payload = output.rsplit(marker, 1)[1].splitlines()[0]
    return {"exit_code": 0, "sha256": digest, "capabilities": json.loads(payload)}


def main() -> int:
    """解析研究入口参数并生成报告；无参数；返回进程退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="专项 YAML 配置路径。")
    parser.add_argument("--phase", required=True, choices=("inspect", "api", "native-xrefs", "native-caps", "catalog", "resolve"), help="研究阶段。")
    args = parser.parse_args()
    config = load_config(args.config)
    phases = {"inspect": (inspect_client, "client_inspection.json"), "api": (probe_apis, "api_probe.json"), "native-xrefs": (inspect_native_xrefs, "native_xrefs.json"), "native-caps": (query_native_caps, "native_caps.json"), "catalog": (probe_catalog, "catalog_probe.json"), "resolve": (resolve_playback, "playback_verification.json")}
    action, name = phases[args.phase]
    report = action(config)
    path = save_report(report, config, name)
    print(path.relative_to(PROJECT_ROOT))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.phase == "resolve":
        return 0 if report["passed"] else 1
    if args.phase == "native-caps":
        return report["exit_code"]
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
