"""通过本机 Node Inspector 读取已打开的优酷样本，并在内存复用实际请求参数做对照。"""

from __future__ import annotations

from local.youku.inspector import LiveInspector, evaluate_player, process_executable
import ctypes
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

import websocket

from lib.native import native_signature, sample_vid, signature_summary
from lib.online import load_session, request_mtop, require_youku_https, summarize_ups






def extract_live_payload(config: dict[str, Any], url: str) -> dict[str, Any]:
    """校验捕获请求的服务、API、版本与精确样本；config 为配置，url 为内存地址；返回解析后的三组参数。"""
    require_youku_https(url)
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    api = config["network"]["ups"]
    if parsed.path.lower() != f"/h5/{api['api'].lower()}/{api['version']}/":
        raise ValueError("实时请求的 UPS 路径与配置不一致")
    if query.get("api") != [api["api"]] or query.get("v") != [api["version"]]:
        raise ValueError("实时请求的 UPS API 或版本与配置不一致")
    if len(query.get("data", [])) != 1:
        raise ValueError("实时请求缺少唯一 data 参数")
    payload = json.loads(query["data"][0])
    groups = {key: json.loads(payload[key]) for key in ("steal_params", "biz_params", "ad_params")}
    if any(not isinstance(value, dict) for value in groups.values()):
        raise ValueError("实时请求的参数组必须为 JSON 对象")
    vid = sample_vid(config)
    # SDK 的真实 steal_params 可以省略 vid；若存在，则必须与 biz_params 同源。
    if groups["biz_params"].get("vid") != vid or ("vid" in groups["steal_params"]
                                                and groups["steal_params"]["vid"] != vid):
        raise ValueError("实时请求与配置样本不一致")
    return groups


def request_summary(groups: dict[str, Any]) -> dict[str, Any]:
    """提取可公开的参数对照值；groups 为内存参数组；返回硬件、平台和能力参数，不含签名、设备 ID 或观看记录。"""
    allowed = {"biz_params": ("chipset", "brand", "btype", "appArch", "pcdnVer", "app_ver", "native_ver",
                              "h265", "play_ability", "close_ability", "preferClarity", "master_m3u8", "drm_type", "enc_ver"),
               "ad_params": ("os", "osv", "needad")}
    return {section: {key: groups[section][key] for key in fields if key in groups[section]
                      and isinstance(groups[section][key], (str, int, float, bool))}
            for section, fields in allowed.items()}




def live_probe(config: dict[str, Any], pid: int) -> dict[str, Any]:
    """读取已打开的目标播放页并复现 UPS；config 含采样及对照规则，pid 为客户端主进程；返回脱敏报告，不操作播放状态。"""
    options = config["live"]
    page_script = Path(__file__).with_name("live_snapshot.js").read_text(encoding="utf-8")
    page_options = {"api": config["network"]["ups"]["api"], "vid": sample_vid(config),
                    "fields": config["cache"]["stream_fields"]}
    with LiveInspector(config, pid) as inspector:
        expression = "(async()=>{const e=process.mainModule.require('electron');const windows=e.webContents.getAllWebContents().filter(w=>w.getURL().startsWith("
        expression += json.dumps(options["player_url_prefix"]) + "));if(windows.length!==1)throw Error('PlayerNotUnique');"
        expression += "return {user_agent:global.userAgent,snapshot:await windows[0].executeJavaScript("
        expression += json.dumps("(" + page_script + ")(" + json.dumps(page_options) + ")") + ")};})()"
        observed = inspector.evaluate(expression)
        snapshot = observed["snapshot"]
        groups = extract_live_payload(config, snapshot.pop("request_url"))
        # 来源只用于选择官方主机；完整 URL 已在前面移除，不放进报告。
        request_api = dict(config["network"]["ups"], base_url=options["base_url"])
        result = {"observed_at_utc": datetime.now(timezone.utc).isoformat(), "sample": config["sample"],
                  "live_player": snapshot, "request_field_names": {key: sorted(value) for key, value in groups.items()},
                  "request_public_params": request_summary(groups),
                  "ups_cases": [], "independent_file_verified": False}
        jar = load_session(config)
        for case in options["cases"]:
            signature = native_signature(config)
            summary = {"name": case["name"], "native": signature_summary(signature),
                       "remove_biz_fields": case["remove_biz_fields"], "ad_fields": case["ad_fields"]}
            result["ups_cases"].append(summary)
            if not signature.get("ckey"):
                summary["probe_error"] = "NativeSignatureUnavailable"
                continue
            steal = dict(groups["steal_params"], ckey=signature["ckey"], client_ts=int(signature["client_ts"]))
            biz = dict(groups["biz_params"], encryptR_client=signature.get("encryptR1"))
            for key in case["remove_biz_fields"]:
                biz.pop(key, None)
            ads = groups["ad_params"] if case["ad_fields"] is None else {
                key: groups["ad_params"][key] for key in case["ad_fields"] if key in groups["ad_params"]}
            payload = {key: json.dumps(value, separators=(",", ":")) for key, value in
                       (("steal_params", steal), ("biz_params", biz), ("ad_params", ads))}
            response = request_mtop(config, jar, request_api, payload, observed["user_agent"])
            summary.update(summarize_ups(config, response))
    result["inspector"] = {"activated_by_probe": inspector.activated,
                            "owned_inspector_closed": inspector.closed_owned_inspector}
    return result
