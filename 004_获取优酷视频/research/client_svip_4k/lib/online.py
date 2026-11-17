"""通过优酷 HTTPS 接口验证当前会话和清晰度，仅输出脱敏元数据，不下载或解密媒体。"""

from local.youku.session import YoukuRedirect, cookie_value, load_session, request_mtop, require_youku_https, response_status
import hashlib
import http.cookiejar
import json
import sqlite3
import time
from datetime import datetime, timezone
from contextlib import closing
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from lib.native import native_decode_metadata, native_signature, sample_vid, signature_summary
from lib.probe import stream_summary
from lib.client_context import load_client_context














def online_probe(config: dict[str, Any]) -> dict[str, Any]:
    """复用本机登录态请求会员、下载档位和 UPS；config 为研究配置；返回仅脱敏元数据，不保存原始响应。"""
    options = config["network"]
    jar = load_session(config)
    package = json.loads((config["client_dir"] / "resources/app/package.json").read_text(encoding="utf-8"))
    version = f"{package['version']}.{package['buildnumber']}"
    ua = options["user_agent"].format(**package)
    vid = sample_vid(config)
    device = cookie_value(jar, options["device_cookie"])
    context = load_client_context(config, version) if any(
        case["user_agent_source"] == "client_log" or case["device_source"] == "client_log"
        or case["client_hardware_fields"] for case in options["ups"]["cases"]) else None
    member = request_mtop(config, jar, options["member"],
                          {"req": json.dumps(options["member"]["req"], separators=(",", ":"))}, ua)
    member_data = member.get("data", {})
    member_result = response_status(member)
    member_result["membership"] = {k: member_data[k] for k in ("is_vip", "member_id") if k in member_data}
    panel_config = options["panel"]
    system = dict(panel_config["system_info"], utdid=device, ver=version)
    panel_payload = dict(panel_config["payload"], vid=vid,
                         system_info=json.dumps(system, separators=(",", ":")))
    panel = request_mtop(config, jar, panel_config, panel_payload, ua)
    model = panel.get("data", {}).get("model", {})
    qualities = model.get("streamInfo", {}).get("videoQualities", []) if isinstance(model, dict) else []
    panel_result = response_status(panel)
    panel_result["video_qualities"] = [{k: q[k] for k in ("qualityCode", "text") if k in q}
                                      for q in qualities if isinstance(q, dict)]
    result = {"sample": config["sample"], "observed_at_utc": datetime.now(timezone.utc).isoformat(),
              "transport": "H5 MTop with desktop parameters; not native MTop",
              "member": member_result, "download_panel": panel_result,
              "independent_file_verified": False}
    if context:
        result["client_context"] = dict(context["summary"], device_matches_cookie=context["device"] == device)
    native = config["native"]
    result["ups_cases"] = []
    for case in options["ups"]["cases"]:
        signature = native_signature(config)
        case_ua = context["user_agent"] if case["user_agent_source"] == "client_log" else ua
        case_device = context["device"] if case["device_source"] == "client_log" else device
        summary = {"name": case["name"], "native": signature_summary(signature),
                   "request_context": {key: case[key] for key in
                                       ("user_agent_source", "device_source", "client_hardware_fields")},
                   "biz_params_configured": {key: case["biz_params"][key] for key in
                                             ("preferClarity", "play_ability", "close_ability", "enc_ver", "master_m3u8")
                                             if key in case["biz_params"]}}
        summary["player_version"] = case["player_version"]
        result["ups_cases"].append(summary)
        if not signature.get("ckey"):
            summary["probe_error"] = "NativeSignatureUnavailable"
            continue
        steal = {"ccode": native["ccode"], "ckey": signature["ckey"], "vid": vid, "utid": case_device,
                 "client_ip": native["client_ip"], "client_ts": int(signature["client_ts"])}
        if case["player_version"]:
            steal["version"] = case["player_version"]
        biz = dict(case["biz_params"], vid=vid, app_ver=case["player_version"] or version,
                   native_ver=version, encryptR_client=signature.get("encryptR1"))
        if case["client_hardware_fields"]:
            biz.update(context["biz_params"])
        payload = {"steal_params": json.dumps(steal, separators=(",", ":")),
                   "biz_params": json.dumps(biz, separators=(",", ":")),
                   "ad_params": json.dumps(options["ups"]["ad_params"], separators=(",", ":"))}
        request_api = dict(options["ups"], base_url=case["base_url"])
        ups = request_mtop(config, jar, request_api, payload, case_ua)
        summary.update(summarize_ups(config, ups))
    return result


def summarize_ups(config: dict[str, Any], ups: dict[str, Any]) -> dict[str, Any]:
    """脱敏 UPS 响应并按需解包元数据；config 为研究配置，ups 为内存响应；返回公开流、能力和状态，不导出授权字段。"""
    summary = response_status(ups)
    envelope = ups.get("data")
    summary["response_encrypted"] = bool(isinstance(envelope, dict) and envelope.get("enc"))
    if summary["response_encrypted"] and isinstance(envelope.get("data"), str):
        decoded = native_decode_metadata(config, envelope["data"])
        metadata = decoded.get("metadata")
        summary["metadata_decode"] = {"success": isinstance(metadata, dict),
                                       "error": decoded.get("error"), "returncode": decoded.get("returncode")}
        if isinstance(metadata, dict):
            ups = dict(ups, data={"data": metadata})
    summary["streams"] = stream_summary(ups, config["cache"]["stream_fields"])
    # UPS 业务错误可能包在 data.data.error 中，仅记录错误代码，不泄露授权内容。
    body = ups.get("data", {})
    if isinstance(body, dict):
        body = body.get("data", body)
        if isinstance(body, dict):
            summary["body_fields"] = sorted(body)
            if isinstance(body.get("error"), dict):
                summary["business_error_code"] = body["error"].get("code")
            if isinstance(body.get("video"), dict):
                summary["video"] = {k: body["video"][k] for k in ("id", "encodeid", "title", "seconds")
                                    if k in body["video"]}
            if isinstance(body.get("controller"), dict):
                summary["controller"] = {k: body["controller"][k] for k in ("hbr4k_open", "startClarity")
                                         if k in body["controller"]}
            if isinstance(body.get("user"), dict):
                user = body["user"]
                summary["ups_user"] = {k: user[k] for k in ("vip", "svip", "is_vip", "is_svip", "member_id")
                                       if k in user and isinstance(user[k], (bool, int, str))}
                summary["ups_user"]["has_uid"] = bool(user.get("uid"))
                summary["ups_user_fields"] = sorted(user)
            for section in ("jaqDebug", "clientAbility", "vip"):
                if isinstance(body.get(section), dict):
                    summary[section + "_fields"] = sorted(body[section])
            if isinstance(body.get("clientAbility"), dict):
                summary["client_capabilities"] = {k: v for k, v in body["clientAbility"].items()
                                                  if isinstance(v, (bool, int, float))}
    return summary
