"""下载 B 站登录账号可用的最高画质播放流和字幕，音视频只复制封装。

入口参数由 parse_args 解释，运行配置从 config.yaml 读取。
进程退出码：0 表示全部完成，1 表示配置或登录等启动失败，2 表示部分下载失败。
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from http.cookiejar import Cookie
import json
import math
import os
from pathlib import Path
import re
import string
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator
from urllib.parse import parse_qs, urlencode, urlsplit

from video_metadata import VideoInfo, ToolError, atomic_text, discover_videos, migrate_legacy, open_video, read_json

API_ROOT = "https://api.bilibili.com"
HOME_URL = "https://www.bilibili.com/"
VIDEO_PATH = re.compile(r"^/video/(?P<id>BV[0-9A-Za-z]{10}|av[0-9]+)(?:/)?$", re.I)


@dataclass(frozen=True)
class Settings:
    """保存已校验的运行配置；路径均为展开环境变量后的绝对 Path。"""

    output_dir: Path
    profile_dir: Path
    ffmpeg: str
    max_title_length: int
    auth_mode: str
    cookie_file: Path
    proxy: str
    channel: str
    login_timeout: float
    poll_interval: float
    request_timeout: float
    format_sort: list[str]
    format_selector: str
    retries: int
    fragment_retries: int
    socket_timeout: float
    all_parts: bool
    auto_merge_parts: bool
    subtitle_languages: list[str]
    include_ai: bool


@dataclass(frozen=True)
class VideoTarget:
    """描述待处理分 P：url 为规范链接，cid 为内容 ID，part 和 total_parts 为序号和总数。"""

    url: str
    cid: int
    part: int
    total_parts: int = 1


@dataclass(frozen=True)
class SubtitleTrack:
    """描述平台字幕：language 为语言码，name 为显示名，is_ai 标记 AI，url 为源地址。"""

    language: str
    name: str
    is_ai: bool
    url: str
    track_id: str


def expand_path(value: str, base: Path) -> Path:
    """展开路径中的用户目录和环境变量；value 为路径文本，base 为相对基准；返回绝对路径。"""
    def replace_percent(match: re.Match[str]) -> str:
        """将匹配到的 %变量% 转为环境值；match 为正则匹配；变量缺失时报告错误。"""
        key = match.group(1)
        if key not in os.environ:
            raise ToolError(f"路径中的环境变量未设置：{key}")
        return os.environ[key]

    try:
        expanded = string.Template(re.sub(r"%([^%]+)%", replace_percent, value)).substitute(os.environ)
    except (KeyError, ValueError) as exc:
        raise ToolError(f"路径环境变量无法展开：{value}") from exc
    result = Path(expanded).expanduser()
    return (result if result.is_absolute() else base / result).resolve()


def load_settings(path: Path) -> Settings:
    """从 path 指定的 YAML 文件读取全部配置；返回 Settings，缺项或非法值时报错。"""
    import yaml

    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        paths, auth, network, browser, download, subtitles = (config[key] for key in ("paths", "auth", "network", "browser", "download", "subtitles"))
        settings = Settings(
            output_dir=expand_path(paths["output_dir"], path.parent),
            profile_dir=expand_path(paths["chrome_profile_dir"], path.parent),
            ffmpeg=paths["ffmpeg"], max_title_length=paths["max_title_length"],
            auth_mode=auth["mode"], cookie_file=expand_path(auth["cookie_file"], path.parent),
            proxy=network["proxy"],
            channel=browser["channel"], login_timeout=browser["login_timeout_seconds"],
            poll_interval=browser["poll_interval_seconds"], request_timeout=browser["request_timeout_seconds"],
            format_sort=download["format_sort"], format_selector=download["format"],
            retries=download["retries"], fragment_retries=download["fragment_retries"],
            socket_timeout=download["socket_timeout_seconds"], all_parts=download["all_parts"],
            auto_merge_parts=download["auto_merge_parts"],
            subtitle_languages=subtitles["languages"], include_ai=subtitles["include_ai"],
        )
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise ToolError(f"无法读取配置 {path}：{exc}") from exc
    for name in ("login_timeout", "poll_interval", "request_timeout", "socket_timeout"):
        value = getattr(settings, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ToolError(f"配置 {name} 必须为大于 0 的有限数字")
    for name in ("retries", "fragment_retries", "max_title_length"):
        value = getattr(settings, name)
        if type(value) is not int or value < (1 if name == "max_title_length" else 0):
            raise ToolError(f"配置 {name} 必须为有效整数")
    if not 10 <= settings.max_title_length <= 80:
        raise ToolError("max_title_length 必须在 10 到 80 之间")
    for name in ("all_parts", "auto_merge_parts", "include_ai"):
        if type(getattr(settings, name)) is not bool:
            raise ToolError(f"配置 {name} 必须为 true 或 false")
    for name in ("format_sort", "subtitle_languages"):
        values = getattr(settings, name)
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
            raise ToolError(f"配置 {name} 必须为非空字符串数组")
    for name in ("channel", "format_selector", "ffmpeg"):
        if not isinstance(getattr(settings, name), str) or not getattr(settings, name):
            raise ToolError(f"配置 {name} 必须为非空字符串")
    if not isinstance(settings.proxy, str):
        raise ToolError("network.proxy 必须为字符串；空字符串表示直连")
    if settings.proxy:
        proxy = urlsplit(settings.proxy)
        if proxy.scheme not in ("http", "https", "socks5") or not proxy.hostname:
            raise ToolError("network.proxy 仅支持 http://、https:// 或 socks5:// 代理地址")
    if settings.auth_mode not in ("cookies", "chrome"):
        raise ToolError("auth.mode 必须为 cookies 或 chrome")
    try:
        for pattern in settings.subtitle_languages:
            if pattern != "all":
                re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"字幕语言正则表达式有误：{exc}") from exc
    return settings


def normalize_url(raw: str) -> str:
    """将 raw 中的 BV/AV 号或分享链接规范化；保留合法 p 参数，只接受普通投稿和 b23.tv。"""
    raw = raw.strip()
    if re.fullmatch(r"BV[0-9A-Za-z]{10}|av[0-9]+", raw, re.I):
        raw = f"{HOME_URL}video/{raw}"
    else:
        match = re.search(r"https?://[^\s<>\"，。；！）)]+", raw)
        if match:
            raw = match.group(0)
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.port:
        raise ToolError("请输入 B 站普通投稿链接、b23.tv 短链接或 BV/AV 号")
    if parsed.hostname == "b23.tv" and parsed.path.strip("/"):
        return f"https://b23.tv{parsed.path}"
    if parsed.hostname not in ("bilibili.com", "www.bilibili.com", "m.bilibili.com"):
        raise ToolError("仅支持 bilibili.com 普通投稿和 b23.tv 短链接")
    match = VIDEO_PATH.fullmatch(parsed.path)
    if not match:
        raise ToolError("首版仅支持 /video/BV... 或 /video/av...，不支持直播、番剧及课程")
    video_id = match["id"]
    video_id = ("BV" + video_id[2:]) if video_id[:2].lower() == "bv" else video_id.lower()
    parts = parse_qs(parsed.query, keep_blank_values=True).get("p")
    query = ""
    if parts:
        if len(parts) != 1 or not parts[0].isdigit() or int(parts[0]) < 1:
            raise ToolError("链接的 p 参数必须为一个正整数")
        query = "?" + urlencode({"p": int(parts[0])})
    return f"{HOME_URL}video/{video_id}{query}"


def api_json(context: Any, path: str, settings: Settings, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """通过登录 context 请求 API path；settings 控制超时、params 为查询参数；返回成功响应数据。"""
    response = context.request.get(
        API_ROOT + path, params=params, headers={"Referer": HOME_URL}, timeout=settings.request_timeout * 1000,
    )
    try:
        if not response.ok:
            raise ToolError(f"B 站接口 HTTP {response.status}：{path}")
        result = response.json()
        if result.get("code") != 0:
            raise ToolError(f"B 站接口 {path} 返回 {result.get('code')}：{result.get('message', '请求失败')}")
        if not isinstance(result.get("data"), dict) and not isinstance(result.get("data"), list):
            raise ToolError(f"B 站接口数据格式异常：{path}")
        return result
    finally:
        response.dispose()


def login_status(context: Any, settings: Settings) -> dict[str, Any] | None:
    """检查 context 的 B 站登录状态；settings 控制超时；有效时返回账户数据，未登录时返回 None。"""
    response = context.request.get(API_ROOT + "/x/web-interface/nav", timeout=settings.request_timeout * 1000)
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
    if isinstance(context, CookieSession):
        raise ToolError("导出的 Cookie 已失效，请在默认 Chrome 中确认 B 站已登录，再点击导出工具重新导出")
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=settings.request_timeout * 1000)
    except Error:
        print("首页自动打开失败，请在专用 Chrome 中刷新或手动访问 bilibili.com 后登录。", flush=True)
    print(f"请在专用 Chrome 窗口登录 B 站，最多等待 {settings.login_timeout:g} 秒。", flush=True)
    deadline = time.monotonic() + settings.login_timeout
    while time.monotonic() < deadline:
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


class CookieSession:
    """使用默认 Chrome 导出的登录 Cookie 访问接口，不启动或读取浏览器数据库。"""

    def __init__(self, pattern: Path, settings: Settings) -> None:
        """从 pattern 加载本机导出，按 settings 创建 HTTP 会话；无返回值。"""
        from yt_dlp import YoutubeDL

        path, self.user_agent, cookies = load_cookie_export(pattern)
        self.settings, self.request = settings, self
        self.downloader = YoutubeDL({"quiet": True, "proxy": settings.proxy, "socket_timeout": settings.request_timeout, "http_headers": {"User-Agent": self.user_agent, "Referer": HOME_URL}})
        inject_cookies(self.downloader, cookies)
        print(f"已读取默认 Chrome 登录态：{path.name}（仅用于本机请求）。")

    def get(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: float | None = None, max_redirects: int | None = None) -> CookieResponse:
        """GET url 并返回响应；params/headers 为查询/请求头，timeout 为毫秒。

        max_redirects 为 Playwright 接口兼容参数，当前使用 yt-dlp 内置重定向上限。
        """
        from yt_dlp.networking import Request

        request = Request(url, query=params, headers={"User-Agent": self.user_agent, "Referer": HOME_URL, **(headers or {})}, extensions={"timeout": timeout / 1000 if timeout else self.settings.request_timeout})
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


def resolve_targets(context: Any, raw: str, settings: Settings, all_parts: bool) -> list[VideoTarget]:
    """解析 raw 分享链接并查询分 P；context 复用登录，settings 控制请求，all_parts 决定是否取全部。"""
    url = normalize_url(raw)
    if urlsplit(url).hostname == "b23.tv":
        response = context.request.get(url, timeout=settings.request_timeout * 1000, max_redirects=5)
        try:
            url = normalize_url(response.url)
        finally:
            response.dispose()
        if urlsplit(url).hostname == "b23.tv":
            raise ToolError("b23.tv 短链接未能解析到普通投稿")
    parsed = urlsplit(url)
    video_id = VIDEO_PATH.fullmatch(parsed.path)["id"]
    params = {"bvid": video_id} if video_id.startswith("BV") else {"aid": video_id[2:]}
    pages = api_json(context, "/x/player/pagelist", settings, params)["data"]
    if not isinstance(pages, list) or not pages:
        raise ToolError("视频没有可下载的分 P，可能已删除或无访问权限")
    requested = int(parse_qs(parsed.query).get("p", ["1"])[0])
    selected = pages if all_parts else [page for page in pages if page.get("page") == requested]
    if not selected:
        raise ToolError(f"视频不存在第 {requested} P（共 {len(pages)} P）")
    return [VideoTarget(f"{HOME_URL}video/{video_id}?p={page['page']}", int(page["cid"]), int(page["page"]), len(pages)) for page in selected]


def make_downloader(context: Any, settings: Settings) -> Any:
    """根据 settings 创建 yt-dlp，并从登录 context 注入内存 Cookie；返回需由调用者关闭的下载器。"""
    from yt_dlp import YoutubeDL

    if isinstance(context, CookieSession):
        user_agent = context.user_agent
    else:
        page = context.pages[0] if context.pages else context.new_page()
        user_agent = page.evaluate("navigator.userAgent")
    downloader = YoutubeDL({
        "format": settings.format_selector, "format_sort": settings.format_sort,
        "noplaylist": True, "retries": settings.retries, "fragment_retries": settings.fragment_retries,
        "socket_timeout": settings.socket_timeout, "continuedl": True, "overwrites": False,
        "skip_unavailable_fragments": False,
        "proxy": settings.proxy,
        "fixup": "never", "postprocessors": [], "writesubtitles": False,
        "writeautomaticsub": False, "http_headers": {"User-Agent": user_agent, "Referer": HOME_URL},
    })
    inject_cookies(downloader, context.cookies([HOME_URL, API_ROOT + "/"]))
    if not any(cookie.name == "SESSDATA" and cookie.value for cookie in downloader.cookiejar):
        downloader.close()
        raise ToolError("登录检查通过但未取得 SESSDATA，请在专用 Chrome 中重新登录")
    return downloader


def list_subtitles(context: Any, target: VideoTarget, info: dict[str, Any], settings: Settings) -> list[SubtitleTrack]:
    """查询 target/info 对应的字幕，按 settings 筛选语言与 AI；返回字幕列表，接口错误不伪装成无字幕。"""
    video_id = str(info["id"]).split("_p")[0]
    data = api_json(context, "/x/player/wbi/v2", settings, {"bvid": video_id, "cid": target.cid})["data"]
    if not isinstance(data, dict):
        raise ToolError("字幕列表格式异常")
    if data.get("need_login_subtitle"):
        raise ToolError("平台要求重新登录后才能获取字幕")
    tracks = []
    for item in data.get("subtitle", {}).get("subtitles", []):
        language = item.get("lan", "")
        url = item.get("subtitle_url", "")
        if not language or language == "danmaku" or not url:
            continue
        is_ai = language.startswith("ai-") or bool(item.get("ai_type"))
        if is_ai and not settings.include_ai:
            continue
        if "all" not in settings.subtitle_languages and not any(re.fullmatch(pattern, language) for pattern in settings.subtitle_languages):
            continue
        tracks.append(SubtitleTrack(language, item.get("lan_doc", language), is_ai, "https:" + url if url.startswith("//") else url, str(item.get("id", ""))))
    return tracks


def safe_name(value: str, limit: int) -> str:
    """将 value 转为 Windows 安全文件名，最多 limit 个字符；保留中文并处理设备保留名。"""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:limit].rstrip(" .") or "video"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", value.split(".")[0], re.I):
        value = "_" + value
    return value


def srt_timestamp(seconds: Any) -> str:
    """将非负、有限的 seconds 秒数转换为毫秒精度 SRT 时间文本；非法时间时报错。"""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        raise ToolError("字幕含非法时间戳")
    milliseconds = int(seconds * 1000 + 0.5)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def json_to_srt(data: dict[str, Any]) -> str:
    """把 B 站 data.body 字幕转换为 SRT 文本；保留文本与时间，不进行翻译或语音识别。"""
    body = data.get("body")
    if not isinstance(body, list):
        raise ToolError("字幕响应缺少 body 列表")
    entries = []
    for item in body:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise ToolError("字幕内容格式异常")
        start, end = item.get("from"), item.get("to")
        start_text, end_text = srt_timestamp(start), srt_timestamp(end)
        if end < start:
            raise ToolError("字幕结束时间早于开始时间")
        entries.append(f"{len(entries) + 1}\n{start_text} --> {end_text}\n{item['content']}\n")
    return "\n".join(entries)


def fetch_subtitles(context: Any, tracks: list[SubtitleTrack], folder: Path, stem: str, settings: Settings) -> list[str]:
    """逐条保存 tracks 到 folder/stem 的 SRT；context/settings 控制请求，返回未完成项，保留成功字幕。"""
    errors = []
    used_names: set[str] = set()
    for track in tracks:
        suffix = safe_name(track.language, 30) + (".ai" if track.is_ai else "")
        if suffix in used_names:
            suffix += "." + safe_name(track.track_id, 20)
        used_names.add(suffix)
        path = folder / f"{stem}.{suffix}.srt"
        if path.is_file() and path.stat().st_size:
            print(f"字幕已存在：{path.name}")
            continue
        try:
            for attempt in range(settings.retries + 1):
                response = None
                try:
                    response = context.request.get(track.url, headers={"Referer": HOME_URL}, timeout=settings.request_timeout * 1000)
                    if not response.ok:
                        raise ToolError(f"HTTP {response.status}")
                    text = json_to_srt(response.json())
                    break
                except Exception:
                    if attempt == settings.retries:
                        raise
                finally:
                    if response is not None:
                        response.dispose()
            atomic_text(path, text)
            print(f"字幕已保存：{path.name}")
        except Exception as exc:
            errors.append(f"字幕 {track.language}（{track.name}）：{exc}")
    return errors


def chosen_formats(info: dict[str, Any]) -> list[dict[str, Any]]:
    """读取 info 中 yt-dlp 已选中的流；返回原编码格式列表，拒绝 DRM 或无地址的格式。"""
    formats = info.get("requested_formats") or [info]
    if any(fmt.get("has_drm") or not (fmt.get("url") or fmt.get("fragments")) for fmt in formats):
        raise ToolError("视频流不可直接下载或受 DRM 保护")
    return formats


def choose_container(formats: list[dict[str, Any]]) -> str:
    """根据 formats 的原始编码选择无损封装容器；常见 MP4 编码返回 mp4，其余返回 mkv。"""
    video = ("avc", "h264", "hev", "hvc", "h265", "hevc", "av01", "av1")
    audio = ("mp4a", "aac", "ac3", "ac-3", "eac3", "ec-3", "mp3", "alac")
    for fmt in formats:
        for key, allowed in (("vcodec", video), ("acodec", audio)):
            codec = fmt.get(key)
            if codec != "none" and (not isinstance(codec, str) or not codec.lower().startswith(allowed)):
                return "mkv"
    return "mp4"


def ffmpeg_command(executable: str, sources: list[Path], formats: list[dict[str, Any]], destination: Path) -> list[str]:
    """为 executable 构造 sources 到 destination 的纯复制命令；formats 决定映射，返回参数数组。"""
    command = [executable, "-hide_banner", "-nostdin", "-y"]
    for source in sources:
        command.extend(["-i", str(source)])
    for index, fmt in enumerate(formats):
        if fmt.get("vcodec") != "none":
            command.extend(["-map", f"{index}:v:0?"])
        if fmt.get("acodec") != "none":
            command.extend(["-map", f"{index}:a:0?"])
    command.extend(["-c", "copy", "-map_metadata", "0", "-map_chapters", "0"])
    if destination.suffix == ".mp4":
        command.extend(["-movflags", "+faststart"])
    return [*command, str(destination)]


def mux_streams(executable: str, sources: list[Path], formats: list[dict[str, Any]], folder: Path, stem: str) -> Path:
    """用 executable 原样合并 sources/formats 至 folder/stem；MP4 失败改试 MKV，返回成功文件路径。"""
    preferred = choose_container(formats)
    errors = []
    for extension in (["mp4", "mkv"] if preferred == "mp4" else ["mkv"]):
        destination = folder / f"{stem}.{extension}"
        temporary = folder / f"{stem}.partial.{extension}"
        command = ffmpeg_command(executable, sources, formats, temporary)
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size:
            temporary.replace(destination)
            return destination
        temporary.unlink(missing_ok=True)
        errors.append(f"{extension}：" + "\n".join(result.stderr.splitlines()[-5:]))
        if extension == "mp4":
            print("MP4 无损封装未成功，尝试 MKV；音视频编码保持不变。")
    raise ToolError("无损封装失败，已保留下载流：\n" + "\n".join(errors))


def resolve_ffmpeg(settings: Settings, config_dir: Path) -> str:
    """解析 settings 中的 FFmpeg 配置；config_dir 为相对路径基准，返回可用可执行文件路径。"""
    if settings.ffmpeg == "auto":
        import imageio_ffmpeg
        executable = Path(imageio_ffmpeg.get_ffmpeg_exe())
    else:
        executable = expand_path(settings.ffmpeg, config_dir)
    if not executable.is_file():
        raise ToolError(f"FFmpeg 不存在：{executable}；请先运行“准备环境”")
    return str(executable)


def print_video_info(downloader: Any, info: dict[str, Any], tracks: list[SubtitleTrack], subtitle_error: str | None) -> None:
    """显示 downloader 解析的 info 画质和 tracks 字幕；subtitle_error 区分接口失败和确实无字幕。"""
    print(f"\n视频：{info['title']} [{info['id']}]", flush=True)
    downloader.list_formats(info)
    for fmt in chosen_formats(info):
        print(f"已选：{fmt.get('format_id')} | {fmt.get('width', '-')}x{fmt.get('height', '-')} | {fmt.get('fps', '-')} fps | 视频 {fmt.get('vcodec')} | 音频 {fmt.get('acodec')}")
    if subtitle_error:
        print(f"字幕列表未完成：{subtitle_error}")
    elif not tracks:
        print("平台没有符合配置的可用字幕；仍会保存视频。")
    for track in tracks:
        print(f"字幕：{track.language} | {track.name} | {'AI' if track.is_ai else '人工'}")


def video_stem(settings: Settings, title: str, video_id: str) -> str:
    """按 settings 的长度限制清理 title，以 _ 连接 BV 号或分 P 标识 video_id，返回无扩展名的名称。"""
    return f"{safe_name(title, settings.max_title_length)}_{safe_name(video_id, 60)}"


def video_folder(settings: Settings, title: str, video_id: str) -> Path:
    """根据 settings、投稿 title 和 BV 号 video_id 返回“标题_BV号”目录路径。"""
    return settings.output_dir / video_stem(settings, title, video_id)


def get_video_info(settings: Settings, bvid: str, title: str | None = None, total_parts: int | None = None) -> VideoInfo:
    """按 settings 定位 bvid 唯一记录并迁移旧文件、单分 P 目录；title/total_parts 为已知投稿信息，返回 VideoInfo。"""
    if title is None and not any(settings.output_dir.glob(f"*_{bvid}")):
        for path in settings.output_dir.rglob("download.json"):
            try:
                record = read_json(path)
            except ToolError:
                if bvid in str(path):
                    raise
                continue
            if re.fullmatch(re.escape(bvid) + r"_p\d+", str(record.get("id", ""))):
                title = record.get("video_title") or re.split(r"\s+p\d+\s+", record["title"], maxsplit=1, flags=re.I)[0]
                break
    record = open_video(settings.output_dir, bvid, safe_name(title, settings.max_title_length) if title else None, total_parts)
    migrate_legacy(record)
    if total_parts is not None:
        record.data["total_parts"] = total_parts
    record.flatten_single_part()
    if title:
        record.data["title"] = title
    return record


def update_source_info(record: VideoInfo, metadata: dict[str, Any]) -> None:
    """将原站 metadata 的投稿标题、UP 主和总分 P 数更新到 record；不写盘，返回 None。"""
    if metadata.get("bvid") != record.data["bvid"]:
        raise ToolError("原站返回的 BV 号与本地记录不一致")
    owner = metadata.get("owner") or {}
    record.data.update(title=metadata["title"], uploader={"id": owner.get("mid"), "name": owner.get("name")} if owner else None,
                       total_parts=len(metadata["pages"]) if metadata.get("pages") else metadata.get("videos", record.data["total_parts"]), errors=[])
    if not owner:
        record.data["errors"].append("原站未提供 UP 主信息")
    if record.data["total_parts"] is None:
        record.data["errors"].append("原站未提供总分 P 数")


def save_video(downloader: Any, info: dict[str, Any], settings: Settings, executable: str, video_title: str, total_parts: int) -> tuple[Path, str]:
    """下载 info 并更新记录；settings/executable 控制封装，video_title/total_parts 为投稿标题和总数，单 P 直接存投稿目录，返回目录和前缀。"""
    formats = chosen_formats(info)
    video_id = safe_name(str(info["id"]), 60)
    base_id = video_id.split("_p")[0]
    record = get_video_info(settings, base_id, video_title, total_parts)
    record.data["total_parts"] = total_parts
    part = int(video_id.rsplit("_p", 1)[1])
    item = record.entry(video_id, part=part, title=info["title"])
    folder = record.path.parent if total_parts == 1 else record.path.parent / video_id
    folder.mkdir(parents=True, exist_ok=True)
    stem = video_stem(settings, info["title"], video_id)
    signature = [{key: fmt.get(key) for key in ("format_id", "vcodec", "acodec", "width", "height", "fps")} for fmt in formats]
    if item["status"] == "completed" and item["formats"] == signature:
        existing = record.resolve_file(item)
        if existing.is_file() and existing.stat().st_size:
            record.complete(item, existing, executable)
            print(f"视频已完成，跳过下载：{existing}")
            return existing.parent, str(item["stem"])
    item.update(title=info["title"], stem=stem, formats=signature, processing="stream copy", status="downloading", errors=[])
    record.save()
    staging = folder / ".streams"
    staging.mkdir(exist_ok=True)
    sources = []
    try:
        for index, fmt in enumerate(formats):
            extension = safe_name(str(fmt.get("ext", "bin")), 10)
            format_id = safe_name(str(fmt.get("format_id", index)), 40)
            source = staging / f"{index}.{format_id}.{extension}"
            stream_info = {**fmt, "id": info["id"], "title": info["title"], "http_headers": {**info.get("http_headers", {}), **fmt.get("http_headers", {})}}
            # dl 仅调用传输层下载器，不运行 yt-dlp 的自动合并、修复或转码后处理。
            success, _ = downloader.dl(str(source), stream_info)
            if not success or not source.is_file() or not source.stat().st_size:
                raise ToolError(f"原始流下载未完成：{source.name}；重新运行可续传")
            sources.append(source)
        destination = mux_streams(executable, sources, formats, folder, stem)
        record.complete(item, destination, executable)
    except (Exception, KeyboardInterrupt) as exc:
        if item["status"] != "completed":
            item.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", errors=[str(exc) or "用户中止"])
            try:
                record.save()
            except OSError as write_error:
                exc.add_note(f"下载状态保存失败：{write_error}")
        raise
    for source in sources:
        source.unlink(missing_ok=True)
    if not any(staging.iterdir()):
        staging.rmdir()
    print(f"视频已保存（原编码，无重新编码）：{destination}")
    return folder, stem


def merge_parts(settings: Settings, executable: str, record: VideoInfo) -> Path:
    """根据 record 中的完成记录按序拼接；settings 控制命名，executable 指定 FFmpeg，更新记录并返回目标路径。"""
    video_id = record.data["bvid"]
    parts: dict[int, tuple[Path, dict[str, Any]]] = {}
    for item in record.data["files"]:
        if item.get("kind") != "part":
            continue
        match = re.fullmatch(re.escape(video_id) + r"_p(\d+)", item["id"])
        if not match or item.get("part") != int(match[1]):
            raise ToolError("下载记录中的分 P 标识无效")
        part = int(match[1])
        if item.get("status") != "completed":
            raise ToolError(f"第 {part} P 尚未完成")
        source = record.resolve_file(item)
        if not source.is_file() or not source.stat().st_size:
            raise ToolError(f"第 {part} P 文件不存在或为空：{source}")
        if part in parts:
            raise ToolError(f"第 {part} P 存在重复记录")
        parts[part] = (source, item)
    if len(parts) < 2:
        raise ToolError(f"{video_id} 至少需要两个已下载分 P 才能拼接")
    total = record.data.get("total_parts")
    if total is not None and (type(total) is not int or total < 1):
        raise ToolError("下载记录中的分 P 总数不一致或无效")
    if total is None:
        print("旧下载记录没有总集数，无法确认最后一 P 是否已下载；将拼接目前连续的分 P。")
        total = max(parts)
    if sorted(parts) != list(range(1, total + 1)):
        raise ToolError(f"分 P 不完整：需要 P1 至 P{total}，实际已有 {sorted(parts)}")
    ordered = [parts[number] for number in sorted(parts)]
    def compatibility(record: dict[str, Any]) -> list[tuple[Any, ...]]:
        """提取 record 的编码和分辨率；返回用于比较的流参数列表。"""
        formats = record.get("formats")
        if not isinstance(formats, list) or not formats or any(not isinstance(fmt, dict) for fmt in formats):
            raise ToolError("下载记录缺少编码信息，无法安全拼接")
        return [(fmt.get("vcodec"), fmt.get("acodec"), fmt.get("width"), fmt.get("height")) for fmt in formats]
    first_signature = compatibility(ordered[0][1])
    if any(compatibility(record) != first_signature for _, record in ordered[1:]):
        raise ToolError("各分 P 的编码或分辨率不同，无法直接无损拼接")
    title = record.data["title"]
    folder = record.path.parent
    folder.mkdir(parents=True, exist_ok=True)
    extension = ".mp4" if all(source.suffix.lower() == ".mp4" for source, _ in ordered) else ".mkv"
    destination = folder / f"{video_stem(settings, title, video_id)}{extension}"
    if destination.exists():
        raise ToolError(f"拼接文件已存在，请先检查或更名：{destination}")
    with tempfile.TemporaryDirectory(prefix=".merge-", dir=folder) as temporary:
        temporary_dir = Path(temporary)
        playlist = temporary_dir / "parts.ffconcat"
        lines = ["file '" + source.resolve().as_posix().replace("'", "'\\''") + "'" for source, _ in ordered]
        playlist.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        output = temporary_dir / ("merged" + extension)
        command = [executable, "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(playlist), "-c", "copy", str(output)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0 or not output.is_file() or not output.stat().st_size:
            raise ToolError("拼接失败，原分 P 已保留：\n" + "\n".join(result.stderr.splitlines()[-10:]))
        if "non-monoton" in result.stderr.lower():
            print("FFmpeg 报告时间戳不连续，请检查拼接点的播放效果。")
        output.replace(destination)
    merged = record.entry(f"{video_id}:merged", "merged", title=title)
    merged.update(formats=ordered[0][1]["formats"], processing="stream copy")
    record.complete(merged, destination, executable)
    print(f"分 P 已拼接（原编码）：{destination}")
    return destination


def refresh_metadata(settings: Settings, args: argparse.Namespace, executable: str) -> int:
    """按 settings 迁移记录、整理单 P 目录并补生成信息；args 提供认证覆盖项，executable 用于文件探测，返回 0 或部分失败码 2。"""
    video_ids, failures = discover_videos(settings.output_dir)
    records = []
    for bvid in video_ids:
        try:
            record = get_video_info(settings, bvid)
            record.refresh_local(executable)
            records.append(record)
        except (OSError, ToolError, KeyError) as exc:
            failures.append(f"{bvid}：{exc}")
    if records:
        try:
            with authenticated_session(settings, args.cookies.expanduser().resolve() if args.cookies else None) as context:
                ensure_login(context, settings)
                for record in records:
                    try:
                        metadata = api_json(context, "/x/web-interface/view", settings, {"bvid": record.data["bvid"]})["data"]
                        update_source_info(record, metadata)
                        record.flatten_single_part()
                    except Exception as exc:
                        record.data["errors"] = [f"原站信息获取失败：{exc}"]
                    record.save()
        except Exception as exc:
            for record in records:
                record.data["errors"] = [f"原站认证或信息保存失败：{exc}"]
                record.save()
    for record in records:
        failures.extend(f"{record.data['bvid']}：{error}" for error in record.data["errors"])
        for item in record.data["files"]:
            failures.extend(f"{item['id']}：{error}" for error in item["errors"])
        print(f"视频信息已更新：{record.path}")
    for failure in failures:
        print(failure, file=sys.stderr)
    return 2 if failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 argv（None 使用进程参数）；返回链接、配置及登录、下载、手动拼接或信息更新模式。"""
    parser = argparse.ArgumentParser(description="B 站最高画质原编码下载；音视频仅无损封装，字幕独立保存")
    parser.add_argument("--url", action="append", help="普通投稿链接、短链接或 BV/AV 号；可重复指定")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.yaml"), help="YAML 配置路径（默认脚本同目录）")
    parser.add_argument("--cookies", type=Path, help="默认 Chrome 导出的 Cookie JSON 文件；覆盖配置中的认证方式和文件路径")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--login-only", action="store_true", help="仅验证配置的登录态；默认读取 Chrome 导出的 Cookie")
    mode.add_argument("--list-formats", action="store_true", help="仅查看可用画质、已选编码和字幕")
    mode.add_argument("--merge-video", metavar="BV号", help="离线拼接已下载的视频分 P，不需要登录")
    mode.add_argument("--refresh-metadata", action="store_true", help="批量迁移并补生成已有下载的 video_info.json，不下载或拼接视频")
    parser.add_argument("--all-parts", action="store_true", help="下载全部分 P，忽略链接中的 p 选择")
    args = parser.parse_args(argv)
    if args.refresh_metadata and (args.url or args.all_parts):
        parser.error("--refresh-metadata 不能与 --url 或 --all-parts 同用")
    return args


def run(args: argparse.Namespace) -> int:
    """执行 args 指定的下载、拼接或信息更新模式；返回 0（完成）或 2（部分失败），启动错误交由 main 处理。"""
    settings = load_settings(args.config.expanduser().resolve())
    if args.refresh_metadata:
        return refresh_metadata(settings, args, resolve_ffmpeg(settings, args.config.resolve().parent))
    if args.merge_video:
        url = normalize_url(args.merge_video)
        match = VIDEO_PATH.fullmatch(urlsplit(url).path)
        if not match or not match["id"].startswith("BV"):
            raise ToolError("手动拼接请输入已下载视频的 BV 号或普通 BV 视频链接")
        video_id = match["id"]
        executable = resolve_ffmpeg(settings, args.config.resolve().parent)
        merge_parts(settings, executable, get_video_info(settings, video_id))
        return 0
    urls = args.url or []
    if not args.login_only and not urls:
        urls = [input("请粘贴 B 站视频链接或 BV/AV 号：").strip()]
    # 先校验输入，避免无效链接触发浏览器或登录流程。
    urls = [normalize_url(url) for url in urls]
    executable = "" if args.login_only or args.list_formats else resolve_ffmpeg(settings, args.config.resolve().parent)
    failures = []
    with authenticated_session(settings, args.cookies.expanduser().resolve() if args.cookies else None) as context:
        account = ensure_login(context, settings)
        vip = account.get("vipStatus", account.get("vip", {}).get("status", 0))
        print(f"登录有效：{account.get('uname', '当前账号')}；大会员：{'有效' if vip else '未开通或已到期'}。")
        print("仅获取当前账号可用画质；平台提示的会员专属档位不会作为游客画质静默下载。")
        if args.login_only:
            print("登录态有效，之后的画质查询和下载会复用当前认证方式。")
            return 0
        processed: set[str] = set()
        for url in urls:
            try:
                targets = resolve_targets(context, url, settings, args.all_parts or settings.all_parts)
                video_title = ""
                if not args.list_formats:
                    resolved_id = VIDEO_PATH.fullmatch(urlsplit(targets[0].url).path)["id"]
                    params = {"bvid": resolved_id} if resolved_id.startswith("BV") else {"aid": resolved_id[2:]}
                    metadata = api_json(context, "/x/web-interface/view", settings, params)["data"]
                    video_title = metadata["title"]
                    if not isinstance(video_title, str) or not video_title:
                        raise ToolError("投稿标题为空，无法创建视频目录")
                    video_record = get_video_info(settings, metadata["bvid"], video_title, targets[0].total_parts)
                    update_source_info(video_record, metadata)
                    video_record.data["total_parts"] = targets[0].total_parts
                    for target in targets:
                        video_record.entry(f"{metadata['bvid']}_p{target.part}", part=target.part)
                    video_record.save()
            except Exception as exc:
                failures.append(f"{url}：{exc}")
                continue
            downloaded: list[str] = []
            for target in targets:
                if target.url in processed:
                    continue
                processed.add(target.url)
                try:
                    ensure_login(context, settings)
                    with make_downloader(context, settings) as downloader:
                        info = downloader.extract_info(target.url, download=False)
                        if not info or info.get("_type", "video") != "video":
                            raise ToolError("链接未解析为单个普通投稿分 P")
                        subtitle_error = None
                        try:
                            tracks = list_subtitles(context, target, info, settings)
                        except Exception as exc:
                            tracks, subtitle_error = [], str(exc)
                            failures.append(f"{target.url} 字幕列表：{exc}")
                        print_video_info(downloader, info, tracks, subtitle_error)
                        if args.list_formats:
                            continue
                        folder, stem = save_video(downloader, info, settings, executable, video_title, target.total_parts)
                        downloaded.append(str(info["id"]))
                        failures.extend(fetch_subtitles(context, tracks, folder, stem, settings))
                except (Exception, KeyboardInterrupt) as exc:
                    if not args.list_formats:
                        try:
                            failed_record = get_video_info(settings, metadata["bvid"])
                            failed_item = failed_record.entry(f"{metadata['bvid']}_p{target.part}", part=target.part)
                            if failed_item["status"] != "completed":
                                failed_item.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                                                   errors=[str(exc) or "用户中止"])
                                failed_record.save()
                        except (OSError, ToolError) as write_error:
                            exc.add_note(f"下载状态更新失败：{write_error}")
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    failures.append(f"{target.url}：{exc}")
            if not args.list_formats and settings.auto_merge_parts and (args.all_parts or settings.all_parts) and len(targets) > 1:
                if len(downloaded) != len(targets):
                    failures.append(f"{url}：部分分 P 未下载完成，跳过自动拼接")
                else:
                    try:
                        merge_parts(settings, executable, get_video_info(settings, metadata["bvid"]))
                    except Exception as exc:
                        failures.append(f"{url} 自动拼接：{exc}")
            if not args.list_formats:
                try:
                    current_record = get_video_info(settings, metadata["bvid"])
                    failures.extend(f"{url} 信息：{error}" for error in current_record.data["errors"])
                    for item in current_record.data["files"]:
                        if item["id"] in downloaded:
                            failures.extend(f"{item['id']} 信息：{error}" for error in item["errors"])
                except (OSError, ToolError) as exc:
                    failures.append(f"{url} 信息：{exc}")
    if failures:
        print("\n以下项目未完成；成功文件及下载进度已保留：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 2
    print("\n全部处理完成。")
    return 0


def main(argv: list[str] | None = None) -> int:
    """运行命令行入口；argv 为可选参数列表，返回进程退出码，向终端报告可操作的错误。"""
    try:
        return run(parse_args(argv))
    except ModuleNotFoundError as exc:
        print(f"缺少依赖 {exc.name}；请在工作区先运行“准备环境”。", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n已中止；下载流和断点文件已保留。", file=sys.stderr)
        return 130
    except EOFError:
        print("没有收到视频链接；请通过工作区输入或 --url 指定链接。", file=sys.stderr)
    except Exception as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
