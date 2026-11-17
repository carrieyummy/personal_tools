"""读取并校验 YAML 配置，统一展开用户目录和环境变量路径。"""

from __future__ import annotations

import math
import os
import re
import string
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ToolError


@dataclass(frozen=True)
class Settings:
    """保存已校验的 YAML 配置；路径为绝对 Path，all_seasons 展开番剧整季，拼接由命令行或清单控制。"""

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
    http_chunk_size: int  # 单次 HTTP 范围请求的最大字节数；0 表示不主动分块。
    socket_timeout: float
    all_parts: bool
    all_seasons: bool  # 番剧展开原站关联季，每季下载全部正片；普通投稿不受影响。
    threads: int  # 原始流下载并发上限，不包含封装、字幕和拼接。
    queue_capacity_gb: float  # 已下载且等待后处理的流容量，按 1024³ 字节换算。
    max_in_flight: int  # 解析、选档、复用检查、封装、字幕及投稿拼接收尾共用的并发上限。
    mix_quality: bool
    priority: str
    subtitle_languages: list[str]
    include_ai: bool
    log_filename: str
    log_refresh_interval: float
    log_progress_interval: float
    log_state_filename: str
    log_port: int
    log_write_retries: int
    log_write_retry_interval: float
    log_final_state_timeout: float  # 等待近期活跃网页确认最终状态的最长秒数。
    cancel_poll_interval: float
    process_terminate_timeout: float
    overwrite_merged: bool  # 来源一致时 False 复用有效成品，True 强制重合并；来源变化始终更新。


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
        paths, auth, network, browser, download, subtitles, logging, runtime, merge = (config[key] for key in ("paths", "auth", "network", "browser", "download", "subtitles", "logging", "runtime", "merge"))
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
            http_chunk_size=download["http_chunk_size_bytes"],
            socket_timeout=download["socket_timeout_seconds"], all_parts=download["all_parts"],
            all_seasons=download['all_seasons'],
            threads=download['threads'],
            queue_capacity_gb=download['queue_capacity_gb'], max_in_flight=config['max_in_flight'],
            mix_quality=download['mix_quality'],
            priority=download['priority'],
            subtitle_languages=subtitles["languages"], include_ai=subtitles["include_ai"],
            log_filename=logging['html_file'], log_refresh_interval=logging['refresh_interval_seconds'],
            log_progress_interval=logging['progress_interval_seconds'],
            log_state_filename=logging['state_file'], log_port=logging['port'],
            log_write_retries=logging['write_retries'], log_write_retry_interval=logging['write_retry_interval_seconds'],
            log_final_state_timeout=logging['final_state_timeout_seconds'],
            cancel_poll_interval=runtime['cancel_poll_interval_seconds'],
            process_terminate_timeout=runtime['process_terminate_timeout_seconds'],
            overwrite_merged=merge['overwrite_existing'],
        )
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise ToolError(f"无法读取配置 {path}：{exc}") from exc
    for name in ("login_timeout", "poll_interval", "request_timeout", "socket_timeout", "log_refresh_interval", "log_progress_interval",
                 "log_write_retry_interval", "log_final_state_timeout", "cancel_poll_interval", "process_terminate_timeout"):
        value = getattr(settings, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ToolError(f"配置 {name} 必须为大于 0 的有限数字")
    for name in ("retries", "fragment_retries", "max_title_length", "log_write_retries"):
        value = getattr(settings, name)
        if type(value) is not int or value < (1 if name == "max_title_length" else 0):
            raise ToolError(f"配置 {name} 必须为有效整数")
    if not 10 <= settings.max_title_length <= 80:
        raise ToolError("max_title_length 必须在 10 到 80 之间")
    if type(settings.http_chunk_size) is not int or settings.http_chunk_size < 0:
        raise ToolError('download.http_chunk_size_bytes 必须为大于或等于 0 的整数')
    if type(settings.threads) is not int or settings.threads < 1:
        raise ToolError('download.threads 必须为正整数')
    if type(settings.max_in_flight) is not int or settings.max_in_flight < 1:
        raise ToolError('max_in_flight 必须为正整数')
    capacity = settings.queue_capacity_gb
    if (isinstance(capacity, bool) or not isinstance(capacity, (int, float))
            or not math.isfinite(capacity) or capacity < 1 / 1024**3
            or not math.isfinite(capacity * 1024**3)):
        raise ToolError('download.queue_capacity_gb 必须为有限正数且至少为 1 字节')
    if type(settings.mix_quality) is not bool:
        raise ToolError('download.mix_quality 必须为 true 或 false')
    if settings.priority not in ('quality', 'merge'):
        raise ToolError('download.priority 必须为 quality 或 merge')
    for filename, suffix, key in ((settings.log_filename, '.html', 'html_file'), (settings.log_state_filename, '.json', 'state_file')):
        if (not isinstance(filename, str) or not filename or any(char in filename for char in '/\\:<>"|?*')
                or any(char.isspace() for char in filename) or Path(filename).suffix.lower() != suffix):
            raise ToolError(f'logging.{key} 必须为不含目录和空格的 {suffix} 文件名')
    if type(settings.log_port) is not int or not 0 <= settings.log_port <= 65535:
        raise ToolError('logging.port 必须为 0～65535 的整数')
    for name in ("all_parts", "all_seasons", "include_ai"):
        if type(getattr(settings, name)) is not bool:
            raise ToolError(f"配置 {name} 必须为 true 或 false")
    if type(settings.overwrite_merged) is not bool:
        raise ToolError('配置 merge.overwrite_existing 必须为 true 或 false')
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
