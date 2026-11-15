"""提供直连或显式代理的 HTTP 传输；限制响应大小，错误和落盘记录不包含签名地址。"""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from ..runtime import check_cancel
from .errors import AddressExpired, PlaybackError, AuthenticationError


def safe_message(value) -> str:
    """将任意 value 转为脱敏错误文本；返回移除 URL、查询参数及长标识的短消息。"""
    text = re.sub(r"https?://\S+", "[地址已隐藏]", str(value))
    text = re.sub(r"(?i)(?:cookie|cKey|vusession|access_token|logintoken)\s*[:=]\s*\S+", "[凭据已隐藏]", text)
    return re.sub(r"[A-Za-z0-9_+/=.-]{24,}", "[标识已隐藏]", text)[:400]


class Transport:
    """每次请求创建独立 opener；config 为主配置，cancel 为可选取消事件。"""

    def __init__(self, config: dict, cancel=None):
        """保存 config/cancel 及协议过期状态码；不发送请求，无返回值。"""
        self.config, self.network, self.cancel = config, config["network"], cancel
        self.expired_codes = config["playback"]["playback"]["expired_http_codes"]
        self.auth_stop = None

    def open(self, url: str, headers: dict | None = None, data: bytes | None = None, *, method: str | None = None):
        """打开 HTTP/S url；headers/data 为附加头/正文，method 可显式指定 HEAD 等方法；返回须关闭的响应。"""
        check_cancel(self.cancel)
        if self.auth_stop is not None and self.auth_stop.is_set():
            raise AuthenticationError("登录已失效，停止发送新请求")
        if urlsplit(url).scheme not in ("https", "http"):
            raise PlaybackError("媒体和接口地址必须使用 HTTP/S")
        proxy = self.network["proxy"]
        opener = build_opener(ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
        try:
            return opener.open(Request(url, data=data, headers={**self.network["headers"], **(headers or {})}, method=method),
                               timeout=self.network["timeout_seconds"])
        except HTTPError as error:
            error.close()
            if error.code in self.expired_codes:
                raise AddressExpired(f"签名或请求失效：HTTP {error.code}") from None
            raise PlaybackError(f"HTTP 请求失败：{error.code}") from None
        except (URLError, OSError):
            raise PlaybackError("网络请求失败或超时") from None

    def text(self, url: str, headers: dict | None = None, data: bytes | None = None) -> str:
        """读取 url 的有界文本；headers/data 传给请求；返回按声明编码解码的字符串。"""
        limit = self.network["max_response_bytes"]
        with self.open(url, headers, data) as response:
            raw = response.read(limit + 1)
            encoding = response.headers.get_content_charset() or "utf-8"
        if len(raw) > limit:
            raise PlaybackError("接口或清单响应超过配置上限")
        try:
            return raw.decode(encoding)
        except (UnicodeError, LookupError):
            raise PlaybackError("接口或清单文本编码无效") from None

    def json(self, endpoint: str, query: dict, headers: dict | None = None) -> dict:
        """GET endpoint 与 query；headers 可携带内存票据；返回 JSON/腾讯 JSONP 对象，解析失败脱敏报错。"""
        text = self.text(endpoint + "?" + urlencode(query), headers)
        match = re.fullmatch(r"\s*(?:QZOutputJson\s*=\s*)?(\{.*\})\s*;?\s*", text, re.S)
        try:
            value = json.loads(match[1] if match else text)
            if not isinstance(value, dict):
                raise ValueError("not object")
            return value
        except (ValueError, TypeError):
            raise PlaybackError("接口未返回有效 JSON 对象") from None
