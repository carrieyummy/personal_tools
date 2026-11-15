"""管理 Chrome 与导出 Cookie 登录态、认证接口会话及登录续期。"""

from __future__ import annotations

import json
import math
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from . import api
from .config import Settings
from .errors import ToolError
from . import runtime
from .errors import check_cancel


def login_status(context: Any, settings: Settings) -> dict[str, Any] | None:
    """检查 context 的 B 站登录状态；settings 控制超时；有效时返回账户数据，未登录时返回 None。"""
    response = context.request.get(api.API_ROOT + "/x/web-interface/nav", timeout=settings.request_timeout * 1000)
    try:
        if not response.ok:
            raise ToolError(f"登录检查失败：HTTP {response.status}")
        result = response.json()
        if result.get("code") == -101:
            return None
        if result.get("code") != 0:
            raise ToolError(f"登录检查失败：{result.get('message', result.get('code'))}")
        data = result.get("data", {})
        return data if data.get("isLogin") else None
    finally:
        response.dispose()


def ensure_login(context: Any, settings: Settings) -> dict[str, Any]:
    """验证 context 登录；按 settings 处理失效 Cookie 或等待专用 Chrome 登录，返回账户数据。"""
    from playwright.sync_api import Error

    account = login_status(context, settings)
    if account:
        return account
    if isinstance(context, AuthenticatedHttpSession):
        raise ToolError("导出的 Cookie 已失效，请在默认 Chrome 中确认 B 站已登录，再点击导出工具重新导出")
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto(api.HOME_URL, wait_until="domcontentloaded", timeout=settings.request_timeout * 1000)
    except Error:
        print("首页自动打开失败，请在专用 Chrome 中刷新或手动访问 bilibili.com 后登录。", flush=True)
    print(f"请在专用 Chrome 窗口登录 B 站，最多等待 {settings.login_timeout:g} 秒。", flush=True)
    deadline = time.monotonic() + settings.login_timeout
    while time.monotonic() < deadline:
        check_cancel()
        # Playwright 的等待同时处理浏览器事件，用户可在此期间完成扫码或验证码。
        page.wait_for_timeout(settings.poll_interval * 1000)
        account = login_status(context, settings)
        if account:
            return account
    raise ToolError("等待登录超时；请重新运行“登录 B 站”，完成后再下载")


@contextmanager
def chrome_session(settings: Settings) -> Iterator[Any]:
    """按 settings 打开独立持久化 Chrome；产生 BrowserContext，结束时关闭并保存登录态。"""
    from playwright.sync_api import Error, sync_playwright

    settings.profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        try:
            network = {"proxy": {"server": settings.proxy}} if settings.proxy else {"args": ["--no-proxy-server"]}
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=settings.profile_dir, channel=settings.channel, headless=False,
                **network,
            )
        except Error as exc:
            raise ToolError("无法启动专用 Chrome。请确认已安装 Chrome，且没有另一任务占用专用用户目录。") from exc
        try:
            yield context
        finally:
            context.close()


def load_cookie_export(pattern: Path) -> tuple[Path, str, list[dict[str, Any]]]:
    """读取 pattern 指定或匹配的最近一次本机导出；返回实际路径、User-Agent 和有效 B 站 Cookie。"""
    candidates = [path for path in pattern.parent.glob(pattern.name) if path.is_file()]
    if not candidates:
        raise ToolError("未找到 B 站 Cookie 文件。请运行工作区任务“B站：打开 Cookie 导出工具”，在默认 Chrome 加载该扩展并导出登录态；下载目录不同可修改 auth.cookie_file。")
    path = max(candidates, key=lambda candidate: candidate.stat().st_mtime_ns)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        user_agent, entries = payload["userAgent"], payload["cookies"]
        if not isinstance(user_agent, str) or not user_agent or not isinstance(entries, list):
            raise ValueError("文件缺少 userAgent 或 cookies")
        cookies = []
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("Cookie 条目格式错误")
            domain = item.get("domain", "")
            if not isinstance(domain, str) or (domain.lstrip(".") != "bilibili.com" and not domain.endswith(".bilibili.com")):
                continue
            if not isinstance(item.get("name"), str) or not isinstance(item.get("value"), str) or not isinstance(item.get("path"), str):
                raise ValueError("Cookie 缺少名称、值或路径")
            expiry = item.get("expirationDate", item.get("expires", -1))
            if not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                raise ValueError("Cookie 到期时间格式错误")
            if expiry > 0 and expiry <= time.time():
                continue
            cookies.append({**item, "expires": expiry, "secure": bool(item.get("secure", False))})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ToolError(f"Cookie 导出文件格式无效：{path.name}；请用本工具的 Chrome 扩展重新导出") from exc
    if not any(item["name"] == "SESSDATA" and item["value"] for item in cookies):
        raise ToolError("导出文件没有有效的 SESSDATA；请在已登录 B 站的默认 Chrome 配置中重新导出")
    return path, user_agent, cookies


def inject_cookies(downloader: Any, cookies: list[dict[str, Any]]) -> None:
    """将 B 站 cookies 注入 downloader 的内存 CookieJar；保留作用域及到期时间，不写入文件。"""
    for item in cookies:
        domain = item["domain"]
        if domain.lstrip(".") != "bilibili.com" and not domain.endswith(".bilibili.com"):
            continue
        expiry = item.get("expires", -1)
        downloader.cookiejar.set_cookie(Cookie(
            version=0, name=item["name"], value=item["value"], port=None, port_specified=False,
            domain=domain, domain_specified=not item.get("hostOnly", not domain.startswith(".")), domain_initial_dot=domain.startswith("."),
            path=item["path"], path_specified=True, secure=item["secure"],
            expires=int(expiry) if expiry > 0 else None, discard=expiry <= 0,
            comment=None, comment_url=None, rest={"HttpOnly": item.get("httpOnly", False)},
        ))


class CookieResponse:
    """将 yt-dlp HTTP 响应适配为本工具的接口响应，关闭时释放网络连接。"""

    def __init__(self, response: Any) -> None:
        """保存 response（yt-dlp Response），暴露状态码和最终 URL；无返回值。"""
        self.response = response
        self.status, self.url = response.status, response.url
        self.ok = 200 <= self.status < 300

    def json(self) -> dict[str, Any]:
        """读取响应并返回 JSON 数据；无参数，非法 JSON 报错。"""
        return json.loads(self.response.read().decode("utf-8-sig"))

    def dispose(self) -> None:
        """关闭响应并释放网络资源；无参数、无返回值。"""
        self.response.close()


@dataclass(frozen=True)
class AuthSnapshot:
    """保存可跨线程复制的认证数据：user_agent 为请求标识，cookies 为 B 站 Cookie 字典元组。"""

    user_agent: str
    cookies: tuple[dict[str, Any], ...]


class AuthenticatedHttpSession:
    """从认证快照建立独立 HTTP 会话；每个下载任务独占下载器与 CookieJar。"""

    def __init__(self, snapshot: AuthSnapshot, settings: Settings) -> None:
        """按 snapshot 的登录快照和 settings 创建本线程 HTTP 会话；无返回值。"""
        from yt_dlp import YoutubeDL

        self.user_agent = snapshot.user_agent
        self.settings, self.request = settings, self
        self.downloader = YoutubeDL({"quiet": True, "proxy": settings.proxy, "socket_timeout": settings.request_timeout, "http_headers": {"User-Agent": self.user_agent, "Referer": api.HOME_URL}})
        runtime.attach_downloader(self.downloader)
        inject_cookies(self.downloader, deepcopy(list(snapshot.cookies)))

    def get(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: float | None = None, max_redirects: int | None = None) -> CookieResponse:
        """GET url 并返回响应；params/headers 为查询/请求头，timeout 为毫秒。

        max_redirects 为 Playwright 接口兼容参数，当前使用 yt-dlp 内置重定向上限。
        """
        from yt_dlp.networking import Request

        request = Request(url, query=params, headers={"User-Agent": self.user_agent, "Referer": api.HOME_URL, **(headers or {})}, extensions={"timeout": timeout / 1000 if timeout else self.settings.request_timeout})
        return CookieResponse(self.downloader.urlopen(request))

    def cookies(self, urls: list[str]) -> list[dict[str, Any]]:
        """返回作用于 urls 的 B 站内存 Cookie，格式兼容 Playwright；不返回其他域名凭据。"""
        domains = {urlsplit(url).hostname for url in urls}
        cookies = []
        for cookie in self.downloader.cookiejar:
            matches = any(
                domain == cookie.domain.lstrip(".")
                or (cookie.domain_specified and domain.endswith("." + cookie.domain.lstrip(".")))
                for domain in domains if domain
            )
            if matches:
                cookies.append({"name": cookie.name, "value": cookie.value, "domain": cookie.domain, "path": cookie.path, "secure": cookie.secure, "expires": cookie.expires if cookie.expires is not None else -1, "hostOnly": not cookie.domain_specified})
        return cookies

    def close(self) -> None:
        """释放 HTTP 会话资源；无参数、无返回值，不保存 Cookie 值。"""
        self.downloader.close()


class CookieSession(AuthenticatedHttpSession):
    """使用本机 Chrome 导出的 Cookie 建立 HTTP 会话，不访问浏览器数据库。"""

    def __init__(self, pattern: Path, settings: Settings) -> None:
        """从 pattern 读取最新导出文件，按 settings 初始化会话；无返回值。"""
        path, user_agent, cookies = load_cookie_export(pattern)
        super().__init__(AuthSnapshot(user_agent, tuple(cookies)), settings)
        print(f"已读取默认 Chrome 登录态：{path.name}（仅用于本机请求）。")


def snapshot_session(context: Any) -> AuthSnapshot:
    """在认证 context 所在线程提取 User-Agent 和 B 站 Cookie；返回不含浏览器对象的深复制快照。"""
    if isinstance(context, AuthenticatedHttpSession):
        user_agent = context.user_agent
    else:
        page = context.pages[0] if context.pages else context.new_page()
        user_agent = page.evaluate('navigator.userAgent')
    cookies = context.cookies([api.HOME_URL, api.API_ROOT + '/'])
    if not isinstance(user_agent, str) or not user_agent or not any(item.get('name') == 'SESSDATA' and item.get('value') for item in cookies):
        raise ToolError('无法取得有效的认证快照，请重新登录或导出 Cookie')
    return AuthSnapshot(user_agent, tuple(deepcopy(cookies)))


@contextmanager
def worker_session(snapshot: AuthSnapshot, settings: Settings) -> Iterator[AuthenticatedHttpSession]:
    """在调用线程按 snapshot/settings 创建独立会话，产生该会话并在任务结束后关闭。"""
    session = AuthenticatedHttpSession(snapshot, settings)
    try:
        yield session
    finally:
        session.close()


@contextmanager
def authenticated_session(settings: Settings, cookie_file: Path | None) -> Iterator[Any]:
    """按 settings 选择认证会话；cookie_file 非空时优先使用该导出文件，产生会话并自动关闭。"""
    if cookie_file is not None or settings.auth_mode == "cookies":
        session = CookieSession(cookie_file or settings.cookie_file, settings)
        try:
            yield session
        finally:
            session.close()
    else:
        with chrome_session(settings) as context:
            yield context
