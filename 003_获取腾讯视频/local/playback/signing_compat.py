"""显式验收签名候选版本；真实调用、链接授权和媒体读取通过后才提交配置。"""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
from threading import Event
import time
from uuid import uuid4

import frida
import pefile
import psutil
import yaml

from ..native_compat import profile_lock
from ..runtime import check_cancel
from .errors import PlaybackError


def candidate_images(config: dict, pid: int | None = None, *, context_modules: list[str] | None = None) -> tuple[int, dict[str, Path]]:
    """按 config 安装目录匹配可选 pid；context_modules 限定额外设备候选模块，返回唯一进程与实际映射文件。"""
    settings = config["playback"]["playback"]["ckey"]
    root = config["playback"]["native"]["dll_root"]
    found = []
    required = tuple(dict.fromkeys([settings["module"], settings["context"]["module"],
                                   *(value["module"] for value in settings["context"]["mirrors"])]))
    names = list(dict.fromkeys([*required, *(context_modules or [])]))
    processes = [psutil.Process(pid)] if pid is not None else psutil.process_iter()
    for process in processes:
        try:
            if process.name() not in settings["process_names"] or not Path(process.exe()).resolve().is_relative_to(root):
                continue
            mappings = process.memory_maps(grouped=True)
            paths = {}
            for name in names:
                values = {Path(item.path).resolve() for item in mappings if Path(item.path).name.lower() == name.lower()}
                if len(values) != 1 or not next(iter(values)).is_relative_to(root):
                    if name in required:
                        break
                    continue
                paths[name] = values.pop()
            else:
                found.append((process.pid, paths))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if len(found) != 1:
        raise PlaybackError("候选验收需要唯一且已加载签名及设备模块的腾讯播放器")
    return found[0]


def inspect_images(settings: dict, paths: dict[str, Path]) -> tuple[dict, list[dict]]:
    """检查 settings 指定 paths 的 x64 导出、CRT free 导入和可写数据区；返回内存候选配置及扫描区。"""
    candidate = deepcopy(settings)
    sections = []
    for name, path in paths.items():
        raw = path.read_bytes()
        with pefile.PE(data=raw) as image:
            if image.FILE_HEADER.Machine != 0x8664 or image.OPTIONAL_HEADER.Magic != 0x20B:
                raise PlaybackError("签名候选仅支持已验证的 Windows x64 ABI")
            if name == settings["module"]:
                exports = [item for item in image.DIRECTORY_ENTRY_EXPORT.symbols
                           if item.name == settings["export"].encode() and not item.forwarder]
                if len(exports) != 1 or not any(section.Characteristics & 0x20000000
                    and section.VirtualAddress <= exports[0].address < section.VirtualAddress + section.Misc_VirtualSize
                    for section in image.sections):
                    raise PlaybackError("候选 DLL 缺少唯一可执行签名导出")
                free = [item.address - image.OPTIONAL_HEADER.ImageBase
                        for entry in image.DIRECTORY_ENTRY_IMPORT if b"crt" in entry.dll.lower()
                        for item in entry.imports if item.name == b"free"]
                if len(free) != 1:
                    raise PlaybackError("候选 DLL 缺少唯一 CRT free 导入")
                candidate.update(sha256=hashlib.sha256(raw).hexdigest(), free_import_rva=free[0],
                                 client_version=path.parent.name)
            if name == settings["context"]["module"]:
                candidate["context"]["sha256"] = hashlib.sha256(raw).hexdigest()
            sections.extend({"module": name, "sha256": hashlib.sha256(raw).hexdigest(),
                             "rva": section.VirtualAddress, "size": section.Misc_VirtualSize}
                            for section in image.sections if section.Characteristics & 0x80000000
                            and not section.Characteristics & 0x20000000 and section.Misc_VirtualSize)
    if not sections:
        raise PlaybackError("设备模块没有可扫描的静态数据区")
    return candidate, sections


def observe_candidate(pid: int, candidate: dict, sections: list[dict], timeout: float, cancel: Event, *, observation: dict) -> dict:
    """只读观察 pid 的真实签名调用；candidate/sections 限定 ABI 及扫描区，timeout/cancel 限制等待，observation 指定帧结构；返回脱敏证据。"""
    source = "const settings=" + json.dumps(candidate) + ";\nconst sections=" + json.dumps(sections) + ";\n"
    source += "const observation=" + json.dumps(observation) + ";\n"
    templates = Path(__file__).with_name("templates")
    source += templates.joinpath("signing_protocol.js").read_text(encoding="utf-8") + "\n"
    source += templates.joinpath("signing_observer.js").read_text(encoding="utf-8")
    messages = Queue()
    session, script = None, None
    diagnostic = "未观察到符合现有 ABI 的正片签名；请在观察期间切换或重新播放正片"

    def receive(message, data):
        """接收 Frida message/data 并排队；不记录原始异常内容或客户端数据，无返回值。"""
        messages.put(message)

    try:
        session = frida.attach(pid)
        script = session.create_script(source)
        script.on("message", receive)
        script.load()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            check_cancel(cancel)
            try:
                message = messages.get(timeout=min(0.2, max(0.001, deadline - time.monotonic())))
            except Empty:
                continue
            if message["type"] == "error":
                raise PlaybackError("候选观察脚本未能验证导出及 CRT 结构，未调用候选函数")
            payload = message.get("payload", {})
            if payload.get("kind") == "limit":
                raise PlaybackError("观察调用次数超过研究配置上限，未更新候选配置")
            if payload.get("kind") == "ready":
                print("开始观察：请在腾讯播放器中切换或重新播放一集有权限的正片。", flush=True)
            elif payload.get("kind") == "observed":
                # 仅接受脚本固定诊断代码，不将原始参数或 SDK 异常写入输出。
                allowed = {"输入字符串或参数地址结构不符", "心跳或非正片调用", "版本、设备、时间或扩展参数数量不符",
                           "扩展整数数组与配置不符", "扩展 JSON 或末尾字符串结构不符", "签名输出指针或长度不符", "签名输出字符结构不符",
                           "设备字符串扫描或返回帧读取异常"}
                reason = payload.get("reason")
                if reason in allowed:
                    diagnostic = f"观察到调用但未通过结构检查：{reason}"
                    print(diagnostic, flush=True)
                    if payload.get("shape"):
                        print("调用参数结构：" + json.dumps(payload["shape"], ensure_ascii=False), flush=True)
            elif payload.get("kind") == "diagnostic":
                matches = payload.get("matches")
                diagnostic = f"设备字符串匹配数为 {matches}，需要重新分析设备上下文 ABI"
                print(diagnostic, flush=True)
            elif payload.get("kind") == "verified":
                offset = candidate["context"]["guid_offset"]
                locations = []
                for value in payload.get("slots", []):
                    slot, module = value.get("rva"), value.get("module")
                    matches = [s for s in sections if type(slot) is int and s["module"] == module and s["rva"] <= slot and slot + 32 <= s["rva"] + s["size"]]
                    if len(matches) != 1 or slot < offset:
                        raise PlaybackError("观察返回的设备字符串不在候选静态数据区")
                    locations.append({"module": module, "sha256": matches[0]["sha256"], "object_rva": slot - offset, "guid_offset": offset})
                if not locations:
                    raise PlaybackError("没有实际验证的设备字符串位置")
                locations.sort(key=lambda value: (value["module"], value["object_rva"]))
                candidate["context"].update(locations[0], mirrors=locations[1:])
                return {"observed_output_length": payload["signature_length"], "string_rva": slot,
                        "locations": locations, "method": payload["method"]}
        raise PlaybackError(diagnostic)
    finally:
        try:
            if script is not None:
                script.unload()
        finally:
            if session is not None:
                session.detach()


def probe_resource(transport, resource, settings: dict) -> dict:
    """按 settings 的字节/时间上限读取 resource；transport 提供 HTTP，返回无地址的字节数及耗时证据。"""
    size = min(settings["sample_bytes"], resource.expected_bytes or settings["sample_bytes"])
    start = resource.byte_range[0] if resource.byte_range else 0
    headers = {"Range": f"bytes={start}-{start + size - 1}"}
    begun, count = time.monotonic(), 0
    with transport.open(resource.url, headers) as response:
        if response.code not in (200, 206):
            raise PlaybackError("候选签名媒体传输未成功")
        if resource.byte_range and (response.code != 206 or not str(response.headers.get("Content-Range", "")).startswith(f"bytes {start}-")):
            raise PlaybackError("候选媒体未返回请求的字节范围")
        while count < size:
            if time.monotonic() - begun > settings["timeout_seconds"]:
                raise PlaybackError("候选签名媒体读取超过研究时间上限")
            block = response.read1(size - count)
            if not block:
                break
            count += len(block)
    if count != size:
        raise PlaybackError("候选签名媒体读取不足研究字节数")
    return {"bytes": count, "seconds": round(time.monotonic() - begun, 3)}


def verify_links(config: dict, research: dict, pid: int) -> list[dict]:
    """用 config 的全部清单链接和实际画音质验证候选 pid；research 限定媒体读取，返回脱敏授权、续签及读取证据。"""
    from .audio import AudioSelector
    from .client import NativeClient
    from .source import resolve
    from .tasks import catalog, requests

    trial = deepcopy(config)
    trial["network"]["timeout_seconds"] = research["transfer_probe"]["timeout_seconds"]
    client = NativeClient(trial)
    client.signer_pid = pid
    entries = []
    for item in requests(trial, require_single_video=False):
        print(f"验证清单链接：{item.video_id or item.series_id}", flush=True)
        tasks = catalog(client, item if item.video_id else replace(item, all_parts=True))
        task = tasks[0]
        if item.video_id:
            matches = [value for value in tasks if value["video_id"] == item.video_id]
            if len(matches) != 1:
                raise PlaybackError("清单链接没有唯一匹配的正片")
            task = matches[0]
        selected = resolve(client, task, AudioSelector(trial["audio"]))
        refreshed = resolve(client, task, AudioSelector(trial["audio"]), expected=selected)
        if refreshed.identity != selected.identity:
            raise PlaybackError("候选签名续签后的媒体来源不一致")
        probes = []
        for playlist in refreshed.manifests:
            resources = [value for value in playlist.resources if not value.initial]
            if not resources:
                raise PlaybackError("候选清单缺少媒体分片")
            probes.append(probe_resource(client.transport, resources[0], research["transfer_probe"]))
        if not probes:
            raise PlaybackError("候选签名没有可验证的媒体清单")
        entries.append({"video_id": task["video_id"], "quality": item.quality,
                        "identity": selected.identity, "refreshed_identity_matches": True, "probes": probes})
    if not entries:
        raise PlaybackError("没有实际验证任何清单链接，拒绝更新配置")
    return entries


def commit_profile(path: Path, original: bytes, candidate: dict) -> None:
    """将已验收 candidate 的允许字段原子写回 path；original 须未改变，保留注释、换行及其他用户配置。"""
    text = original.decode("utf-8-sig")
    root = yaml.compose(text)
    edits = []
    allowed = (("sha256",), ("free_import_rva",), ("client_version",),
               ("context", "module"), ("context", "sha256"), ("context", "object_rva"), ("context", "mirrors"))
    for keys in allowed:
        node, value = root, candidate
        for key in keys:
            matches = [child for name, child in node.value if name.value == key]
            if len(matches) != 1:
                raise PlaybackError("签名配置字段缺失或重复，拒绝自动更新")
            node, value = matches[0], value[key]
        if isinstance(value, list):
            replacement = yaml.safe_dump(value, allow_unicode=True, default_flow_style=True, width=100000).strip()
        elif isinstance(value, int):
            replacement = hex(value) if text[node.start_mark.index:node.end_mark.index].lower().startswith("0x") else str(value)
        else:
            replacement = "'" + str(value).replace("'", "''") + "'"
        edits.append((node.start_mark.index, node.end_mark.index, replacement))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    updated = (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != original:
            raise PlaybackError("签名配置在验收期间被修改，未写回候选版本")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_candidate(config: dict, research: dict, profile_path: Path, pid: int | None, cancel: Event) -> dict:
    """按 config/research 在可选 pid 上验收并提交 profile_path；cancel 可取消，返回不含凭据的验收报告。"""
    print("正在获取签名配置验收锁并检查候选 DLL。", flush=True)
    with profile_lock(profile_path, cancel, config["runtime"]["cancel_poll_interval_seconds"]):
        original = profile_path.read_bytes()
        settings = config["playback"]["playback"]["ckey"]
        if yaml.safe_load(original.decode("utf-8-sig")) != settings:
            raise PlaybackError("签名配置已变化，请重新加载后验收")
        modules = research["native_observation"]["signing"]["constant_modules"]
        target, paths = candidate_images(config, pid, context_modules=modules)
        candidate, sections = inspect_images(settings, paths)
        evidence = observe_candidate(target, candidate, sections, research["native_observation"]["watch_seconds"], cancel,
                                     observation={**research["native_observation"]["signing"],
                                                  "max_events": research["native_observation"]["max_events"]})
        print("已核对真实调用和唯一设备字符串，开始验证清单链接。", flush=True)
        check_cancel(cancel)
        trial = deepcopy(config)
        trial["playback"]["playback"]["ckey"] = candidate
        entries = verify_links(trial, research, target)
        check_cancel(cancel)
        current_pid, current_paths = candidate_images(config, target, context_modules=modules)
        current, current_sections = inspect_images(settings, current_paths)
        digests = {section["module"]: section["sha256"] for section in current_sections}
        locations = [candidate["context"], *candidate["context"]["mirrors"]]
        if current_pid != target or current_paths != paths or any(current[key] != candidate[key] for key in ("sha256", "free_import_rva", "client_version")) or any(digests.get(value["module"]) != value["sha256"] for value in locations):
            raise PlaybackError("验收期间客户端 DLL 或进程已变化，未更新配置")
        commit_profile(profile_path, original, candidate)
        return {"status": "verified", "profile_updated": True, "client_version": candidate["client_version"],
                "sha256": candidate["sha256"], "context_module": candidate["context"]["module"], "context_sha256": candidate["context"]["sha256"],
                "free_import_rva": candidate["free_import_rva"], "object_rva": candidate["context"]["object_rva"],
                "observation": evidence, "entries": entries}
