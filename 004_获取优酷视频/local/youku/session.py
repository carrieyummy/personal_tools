"""优酷 Cookie 只读快照及 MTop 请求；敏感数据只在内存中使用。"""
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

def require_youku_https(url: str) -> None:
    """限制请求目的地；url 为接口或重定向 URL；仅优酷 HTTPS 可通过，其他情况抛 ValueError。"""
    target = urlsplit(url)
    host = target.hostname or ""
    if (target.scheme != "https" or target.username or target.password or target.port not in (None,443)
            or not (host == "youku.com" or host.endswith(".youku.com"))):
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
    opener = build_opener(ProxyHandler({"http":options["proxy"],"https":options["proxy"]} if options.get("proxy") else {}), HTTPCookieProcessor(jar), YoukuRedirect())
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
