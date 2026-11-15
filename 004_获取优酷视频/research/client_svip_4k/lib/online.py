"""通过优酷 HTTPS 接口验证当前会话和清晰度，仅输出脱敏元数据，不下载或解密媒体。"""

import hashlib
import http.cookiejar
import json
import sqlite3
import time
from contextlib import closing
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from lib.native import native_decode_metadata, native_signature, sample_vid, signature_summary
from lib.probe import stream_summary


def require_youku_https(url: str) -> None:
    """限制请求目的地；url 为接口或重定向 URL；仅优酷 HTTPS 可通过，其他情况抛 ValueError。"""
    target = urlsplit(url)
    host = target.hostname or ""
    if target.scheme != "https" or not (host == "youku.com" or host.endswith(".youku.com")):
        raise ValueError("会话请求只允许 youku.com 的 HTTPS 地址")


class YoukuRedirect(HTTPRedirectHandler):
    """校验每个重定向的目的地；继承标准处理器，无额外构造参数。"""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        """校验后继续重定向；参数来自 urllib 标准接口；返回新请求或 None，非法目的地抛 ValueError。"""
        require_youku_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def load_session(config: dict[str, Any]) -> http.cookiejar.CookieJar:
    """只读装载优酷 Cookie；config 含 user_data；返回仅驻留内存的 CookieJar，不复制数据库或导出文件。"""
    jar = http.cookiejar.CookieJar()
    path = config["user_data"] / "Cookies"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        rows = db.execute("SELECT host_key, name, value, path, is_secure, expires_utc, length(encrypted_value) "
                          "FROM cookies WHERE host_key='youku.com' OR host_key LIKE '%.youku.com'").fetchall()
    for domain, name, value, path, secure, expiry, encrypted in rows:
        if not value:
            if encrypted:
                raise ValueError("遇到加密会话值；本脚本不解密登录数据库")
            continue
        expires = int(expiry / 1_000_000 - 11644473600) if expiry else None
        jar.set_cookie(http.cookiejar.Cookie(0, name, value, None, False, domain, True,
                                            domain.startswith("."), path, True, bool(secure),
                                            expires, expires is None, None, None, {}, False))
    return jar


def cookie_value(jar: http.cookiejar.CookieJar, name: str) -> str:
    """取得优酷公共域 Cookie；jar 为内存会话，name 为配置名称；返回值仅供内存请求使用，缺失抛 ValueError。"""
    values = [c.value for c in jar if c.name == name and c.domain.lstrip(".") == "youku.com"]
    if len(values) != 1:
        raise ValueError(f"缺少唯一公共域 Cookie：{name}")
    return values[0]


def request_mtop(config: dict[str, Any], jar: http.cookiejar.CookieJar, api: dict[str, Any],
                 payload: dict[str, Any], user_agent: str) -> dict[str, Any]:
    """发起一次 MTop 请求；config/jar 为配置和会话，api 含名称版本，payload 为请求体，user_agent 为 UA；返回原始内存 JSON。"""
    options = config["network"]
    base = api.get("base_url", options["base_url"]).rstrip("/")
    require_youku_https(base)
    require_youku_https(options["referer"])
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    timestamp = str(int(time.time() * 1000))
    token = cookie_value(jar, options["token_cookie"]).split("_", 1)[0]
    signed = "&".join((token, timestamp, options["app_key"], data))
    params = {"jsv": options["jsv"], "appKey": options["app_key"], "t": timestamp,
              "sign": hashlib.md5(signed.encode("utf-8")).hexdigest(), "api": api["api"],
              "v": api["version"], "type": options["data_type"], "dataType": "json", "data": data}
    url = f"{base}/{api['api'].lower()}/{api['version']}/?{urlencode(params)}"
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(jar), YoukuRedirect())
    req = Request(url, headers={"User-Agent": user_agent, "Referer": options["referer"],
                               "Accept": "application/json"})
    try:
        with opener.open(req, timeout=options["timeout_seconds"]) as response:
            raw = response.read(options["max_response_bytes"] + 1)
            if len(raw) > options["max_response_bytes"]:
                return {"probe_error": "ResponseTooLarge"}
            return json.loads(raw)
    except HTTPError as error:
        return {"probe_error": "HTTPError", "http_status": error.code}
    except (URLError, TimeoutError, UnicodeError, ValueError) as error:
        # 异常消息可能带含签名的完整请求 URL，不写入报告。
        return {"probe_error": type(error).__name__}


def response_status(response: dict[str, Any]) -> dict[str, Any]:
    """脱敏接口状态；response 为内存原始响应；返回 ret 代码及网络错误类型，不返回原始响应。"""
    ret = response.get("ret", [])
    if not isinstance(ret, list):
        ret = []
    return {"ret_codes": [str(s).split("::", 1)[0] for s in ret],
            "probe_error": response.get("probe_error"), "http_status": response.get("http_status")}


def online_probe(config: dict[str, Any]) -> dict[str, Any]:
    """复用本机登录态请求会员、下载档位和 UPS；config 为研究配置；返回仅脱敏元数据，不保存原始响应。"""
    options = config["network"]
    jar = load_session(config)
    package = json.loads((config["client_dir"] / "resources/app/package.json").read_text(encoding="utf-8"))
    version = f"{package['version']}.{package['buildnumber']}"
    ua = options["user_agent"].format(**package)
    vid = sample_vid(config)
    device = cookie_value(jar, options["device_cookie"])
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
    result = {"sample": config["sample"], "transport": "H5 MTop with desktop parameters; not native MTop",
              "member": member_result, "download_panel": panel_result,
              "independent_file_verified": False}
    native = config["native"]
    result["ups_cases"] = []
    for case in options["ups"]["cases"]:
        signature = native_signature(config)
        summary = {"name": case["name"], "native": signature_summary(signature)}
        result["ups_cases"].append(summary)
        if not signature.get("ckey"):
            summary["probe_error"] = "NativeSignatureUnavailable"
            continue
        steal = {"ccode": native["ccode"], "ckey": signature["ckey"], "vid": vid, "utid": device,
                 "client_ip": native["client_ip"], "client_ts": int(signature["client_ts"])}
        if case["player_version"]:
            steal["version"] = case["player_version"]
        biz = dict(case["biz_params"], vid=vid, app_ver=case["player_version"] or version,
                   native_ver=version, encryptR_client=signature.get("encryptR1"))
        payload = {"steal_params": json.dumps(steal, separators=(",", ":")),
                   "biz_params": json.dumps(biz, separators=(",", ":")),
                   "ad_params": json.dumps(options["ups"]["ad_params"], separators=(",", ":"))}
        request_api = dict(options["ups"], base_url=case["base_url"])
        ups = request_mtop(config, jar, request_api, payload, ua)
        summary.update(response_status(ups))
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
    return result
