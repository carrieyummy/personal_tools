"""复用本机芒果登录态访问原生接口；凭据和签名地址仅保留在请求内存中。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import re
from threading import Event, Lock
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

from .errors import AddressExpired, AuthenticationError, Cancelled, PlaybackDenied, ToolError


def check_cancel(event: Event | None) -> None:
    """检查取消信号；event 为可选线程事件；已取消时抛出 Cancelled，否则无返回值。"""
    if event is not None and event.is_set():
        raise Cancelled("任务已取消，下载现场已保留")


def safe_message(value: Any) -> str:
    """脱敏错误文字；value 为任意说明；返回移除网络地址和长标识后的短文本。"""
    text = re.sub(r"https?://\S+", "[地址已隐藏]", str(value))
    return re.sub(r"[A-Za-z0-9_+/=.-]{20,}", "[标识已隐藏]", text)[:400]


def load_user(path: Path) -> dict[str, Any]:
    """只读解析客户端登录文件；path 为 Base64 JSON 文件；返回含票据的内存对象，不保存或打印。"""
    try:
        user = json.loads(base64.b64decode(path.read_bytes().strip(), validate=True))
    except (OSError, ValueError, UnicodeError) as error:
        raise AuthenticationError("无法读取客户端登录态，请先在芒果客户端登录") from error
    if not isinstance(user, dict) or not isinstance(user.get("ticket"), str) or not user["ticket"] or not user.get("uuid"):
        raise AuthenticationError("客户端登录态缺少有效票据，请重新登录")
    return user


def normalize_url(value: str) -> tuple[str, str, str]:
    """解析官方视频链接；value 为 HTTP/S 分享地址；返回规范链接、clipId、videoId，其他输入报错。"""
    parsed = urlsplit(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not (parsed.hostname == "mgtv.com" or parsed.hostname.endswith(".mgtv.com")):
        raise ToolError("请输入芒果官方 HTTP/S 视频链接")
    match = re.fullmatch(r"/b/(\d+)/(\d+)\.html/?", parsed.path)
    if not match:
        raise ToolError("当前入口需要包含剧集及视频编号的 /b/…/….html 链接")
    clip_id, video_id = match.groups()
    return f"https://www.mgtv.com/b/{clip_id}/{video_id}.html", clip_id, video_id


class Transport:
    """无共享 Cookie 状态的 HTTP 传输；network 提供代理、请求头、超时和响应上限，expired_codes 指定失效状态。"""

    def __init__(self, network: dict[str, Any], expired_codes: list[int], cancel: Event | None = None):
        """保存 network、expired_codes 和可选 cancel；不打开连接，无返回值。"""
        self.network, self.expired_codes, self.cancel = network, expired_codes, cancel

    def opener(self):
        """创建本次请求的独立 opener；无参数；空代理强制直连，返回 urllib opener。"""
        proxy = self.network["proxy"]
        return build_opener(ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))

    def open(self, url: str, headers: dict[str, str] | None = None) -> BinaryIO:
        """打开媒体请求；url 仅留内存，headers 为附加 Range 等头；返回须关闭的响应，错误不含地址。"""
        check_cancel(self.cancel)
        if urlsplit(url).scheme not in ("https", "http"):
            raise ToolError("媒体资源必须使用 HTTP/S")
        request = Request(url, headers={**self.network["headers"], **(headers or {})})
        try:
            return self.opener().open(request, timeout=self.network["timeout_seconds"])
        except HTTPError as error:
            error.close()
            if error.code in self.expired_codes:
                raise AddressExpired(f"媒体地址失效：HTTP {error.code}") from None
            raise ToolError(f"媒体请求失败：HTTP {error.code}") from None
        except (URLError, OSError) as error:
            raise ToolError(f"媒体网络请求失败：{type(error).__name__}") from None

    def json(self, endpoint: str, query: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """请求 JSON；endpoint 为接口地址，query 含仅驻留内存的参数；返回响应字典和脱敏摘要。"""
        check_cancel(self.cancel)
        request = Request(endpoint + "?" + urlencode(query), headers=self.network["headers"])
        try:
            response = self.opener().open(request, timeout=self.network["timeout_seconds"])
        except HTTPError as error:
            response = error
        except (URLError, OSError) as error:
            raise ToolError(f"接口网络请求失败：{type(error).__name__}") from None
        with response:
            raw = response.read(self.network["max_json_bytes"] + 1)
            status = response.code
        if len(raw) > self.network["max_json_bytes"]:
            raise ToolError("接口响应超过配置上限")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ToolError(f"接口未返回有效 JSON：HTTP {status}") from None
        if not isinstance(value, dict):
            raise ToolError("接口 JSON 必须为对象")
        summary = {"http_status": status, "code": value.get("code"), "message": safe_message(value.get("msg", ""))}
        return value, summary


def source_request(profile: dict[str, Any], user: dict[str, Any], clip_id: str, video_id: str, transport: Transport, definition: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """构造原生请求；profile 为协议，user 为登录态，编号为数字串，transport 为传输，definition 可指定档位；返回数据和摘要。"""
    context = {"video_id": video_id, "clip_id": clip_id, "ticket": user["ticket"], "user_uuid": user["uuid"]}
    query = {key: str(value).format_map(context) for key, value in profile["query"].items()}
    query["exdef"] = json.dumps(profile["exdef"], ensure_ascii=False, separators=(",", ":"))
    if definition is not None:
        query[profile["selection_parameter"]] = definition
    return transport.json(profile["endpoint"], query)


class NativeClient:
    """正式流程的原生客户端；config 提供协议与认证路径，cancel 提供取消；所有登录文件只读。"""

    def __init__(self, config: dict[str, Any], cancel: Event | None = None):
        """初始化 config、cancel 及认证互斥锁；不请求网络，无返回值。"""
        self.config, self.profile, self.cancel = config, config["profile"], cancel
        self.transport = Transport(config["network"], self.profile["status"]["expired_http_codes"], cancel)
        self.auth_lock = Lock()

    def user(self) -> dict[str, Any]:
        """取得最新本机登录快照；无参数；返回内存对象，锁内只读文件。"""
        with self.auth_lock:
            return load_user(self.config["auth"]["user_data"])

    def raw_source(self, clip_id: str, video_id: str, definition: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """查询完整响应；clip_id/video_id 为编号，definition 可指定档位；返回数据和摘要，供研究复用。"""
        return source_request(self.profile["playback"], self.user(), clip_id, video_id, self.transport, definition)

    def source(self, clip_id: str, video_id: str, definition: str | None = None) -> dict[str, Any]:
        """取得已授权正片；clip_id/video_id 为编号，definition 可指定档位；返回 data，未授权时明确报错。"""
        value, summary = self.raw_source(clip_id, video_id, definition)
        data = self.authenticated_data(value, summary)
        if value.get("code") not in self.profile["status"]["success_codes"]:
            raise PlaybackDenied(f"完整播放不可用（{value.get('code')}）：{summary['message']}")
        return data

    def authenticated_data(self, value: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
        """核对认证；value/summary 为原生响应和传输摘要；返回有效票据对应的 data，不将单集付费限制当作登录失效。"""
        if summary["http_status"] in self.profile["status"]["authentication_http_codes"]:
            raise AuthenticationError("服务器拒绝当前登录认证，请在客户端重新登录")
        data = value.get("data")
        if not isinstance(data, dict):
            raise ToolError("播放接口缺少 data")
        account = data.get("user", {})
        if account.get("tstatus") not in self.profile["status"]["valid_ticket_states"]:
            raise AuthenticationError("服务器未接受当前登录票据，请在客户端重新登录")
        return data

    def login_status(self, clip_id: str, video_id: str) -> dict[str, Any]:
        """只验证登录；clip_id/video_id 为配置的探测编号；返回账号状态，不要求该集具备完整播放权限。"""
        value, summary = self.raw_source(clip_id, video_id)
        return self.authenticated_data(value, summary)["user"]

    def catalog_page(self, clip_id: str, page: int) -> dict[str, Any]:
        """读取选集页；clip_id 为剧集编号，page 为从 1 开始页号；返回 data，业务失败时报错。"""
        settings = self.profile["catalog"]
        query = {key: str(value).format_map({"clip_id": clip_id}) for key, value in settings["query"].items()}
        query[settings["page_parameter"]] = page
        value, summary = self.transport.json(settings["endpoint"], query)
        if summary["http_status"] in self.profile["status"]["authentication_http_codes"]:
            raise AuthenticationError("选集接口拒绝当前登录认证")
        if value.get("code") not in self.profile["status"]["success_codes"] or not isinstance(value.get("data"), dict):
            raise ToolError(f"选集查询失败：{summary['message']}")
        return value["data"]

    def dispatch(self, stream: dict[str, Any]) -> dict[str, Any]:
        """取得同一 stream 的清单调度；参数为已授权具体源；返回含内存 disp.info 的副本，不切换档位/编码。"""
        embedded = stream.get("disp", {})
        if isinstance(embedded, dict) and isinstance(embedded.get("info"), str) and embedded["info"].startswith(("https://", "http://")):
            return stream
        address = stream.get("url")
        if not isinstance(address, str) or urlsplit(address).scheme not in ("http", "https"):
            raise ToolError("选定源缺少可用调度地址")
        parsed = urlsplit(address)
        settings = self.profile["dispatch"]
        endpoint = urlunsplit((settings["scheme"], parsed.netloc, parsed.path, "", ""))
        value, summary = self.transport.json(endpoint, dict(parse_qsl(parsed.query, keep_blank_values=True)))
        if summary["http_status"] != 200 or value.get("status") not in settings["success_states"] or not isinstance(value.get("info"), str) or not value["info"].startswith(("https://", "http://")):
            raise ToolError(f"选定源调度失败：HTTP {summary['http_status']} / {value.get('status')}")
        return {**stream, "disp": value}
