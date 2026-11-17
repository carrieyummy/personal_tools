"""加载主配置和优酷专项配置；路径、运行值与校验集中管理，不另设隐式默认值。"""

from datetime import date
import math
import os
from pathlib import Path
import re
from string import Formatter

import yaml

from .storage import validate_lock_file


def resolve_path(value: str, base: Path) -> Path:
    """展开字符串 value 的用户目录和环境变量，以 base 为相对基准；返回绝对 Path。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("路径必须为非空字符串")
    path = Path(os.path.expandvars(value)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def read_yaml(path: Path) -> dict:
    """读取 UTF-8 YAML 文件 path；返回映射，拒绝其他根类型。"""
    result = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict):
        raise ValueError("YAML 根节点必须为对象")
    return result


def positive(value, name: str, *, integer: bool = False, zero: bool = False) -> None:
    """验证 value 为有限正数或允许零；name 用于错误定位，integer 限制整数；无返回值。"""
    if (type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value)
            or (value < 0 if zero else value <= 0)):
        raise ValueError(f"{name} 数值无效")


def safe_name(value: str) -> str:
    """将源站 value 清理为无空白的单层 Windows 文件名；空值与保留设备名明确拒绝。"""
    result = re.sub(r'[\s<>:"/\\|?*\x00-\x1f]+', "_", str(value)).strip(". _")
    if not result or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", result):
        raise ValueError("源站名称不能形成有效文件名")
    return result


def series_directory_name(config: dict, series: str, series_id: str, release_date: str) -> str:
    """按 config 命名完整节目；series/series_id 为源站标题和编号，release_date 为 ISO 日期；返回单层目录名。"""
    date.fromisoformat(release_date)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date):
        raise ValueError("上映日期必须为 YYYY-MM-DD")
    name = config["naming"]["series_template"].format(
        title=safe_name(series), series_id=safe_name(series_id), release_date=release_date)
    if safe_name(name) != name:
        raise ValueError("剧集目录模板产生非法名称")
    return name


def load_config(path: Path) -> dict:
    """读取主配置 path 并加载相对专项配置、校验语言优先级；返回独立运行快照，不修改文件。"""
    path = path.expanduser().resolve()
    config = read_yaml(path)
    validate_lock_file(config["logging"]["lock_file"])
    if config["source"] != "url":
        raise ValueError("当前只支持 source: url")
    for key in ("default_list", "playback_profile", "native_profile", "output_path", "work_dir", "ffmpeg_path"):
        config[key] = resolve_path(config[key], path.parent)
    profile = read_yaml(config["playback_profile"])
    languages = profile["audio_language"]
    if (not isinstance(languages, list) or not languages
            or any(not isinstance(value, str) or not value.strip() for value in languages)):
        raise ValueError("audio_language 必须为按优先级排列的非空语言字符串列表")
    for key in ("client_dir", "user_data"):
        profile[key] = resolve_path(profile[key], config["playback_profile"].parent)
    context = profile["request"]["context"]
    for section, keys in (("adapter", ("environment", "support_type", "default_support_type")),
                          ("biz_params", ("media_type", "extag", "pn", "playMode"))):
        for key in keys:
            if not isinstance(context[section][key], str) or not context[section][key].strip():
                raise ValueError(f"request.context.{section}.{key} 必须为非空字符串")
    for key in ("master_m3u8", "wdm"):
        positive(context["biz_params"][key], f"request.context.biz_params.{key}", integer=True, zero=True)
    profile["restoration"] = read_yaml(config["native_profile"])
    for key in ("library_sha256", "challenge_module_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", profile["restoration"][key]):
            raise ValueError("原生适配摘要格式无效")
    library = (profile["client_dir"] / profile["restoration"]["library"]).resolve()
    if not library.is_relative_to(profile["client_dir"]):
        raise ValueError("原生库路径越界")
    for key in ("function_rva", "output_bytes", "timeout_seconds"):
        positive(profile["restoration"][key], key, integer=True)
    if not 25 <= profile["restoration"]["output_bytes"] <= 4096:
        raise ValueError("原生输出缓冲范围无效")
    for section, keys in (("live", ("timeout_seconds", "max_result_bytes", "connect_attempts", "close_delay_ms", "retry_interval_seconds")),
                          ("native", ("timeout_seconds",)), ("catalog", ("max_pages",)),
                          ("media", ("max_manifest_bytes", "max_segment_bytes"))):
        for key in keys:
            positive(profile[section][key], f"{section}.{key}")
    from urllib.parse import urlsplit
    endpoint = urlsplit(profile["live"]["inspector_url"])
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port:
        raise ValueError("Inspector 必须配置明确的回环端口")
    for key in ("process_timeout_seconds", "copy_buffer_bytes", "max_in_flight"):
        positive(config[key], key, integer=True)
    if config["copy_buffer_bytes"] >= 2**31:
        raise ValueError("copy_buffer_bytes 过大")
    for key in ("quality", "threads", "refresh_attempts"):
        positive(config["download"][key], f"download.{key}", integer=True)
    positive(config["download"]["retries"], "download.retries", integer=True, zero=True)
    positive(config["download"]["queue_capacity_gb"], "download.queue_capacity_gb")
    positive(config["audio"]["quality"], "audio.quality", integer=True)
    for section, keys in (("authentication", ("refresh_interval_seconds", "recovery_attempts")),
                          ("network", ("timeout_seconds", "max_response_bytes")),
                          ("logging", ("refresh_interval_seconds", "progress_interval_seconds", "final_state_timeout_seconds")),
                          ("runtime", ("cancel_poll_interval_seconds", "process_terminate_timeout_seconds")),
                          ("verification", ("fps_tolerance",))):
        for key in keys:
            positive(config[section][key], f"{section}.{key}", integer=key in ("recovery_attempts", "max_response_bytes"))
    positive(config["duration_tolerance_seconds"], "duration_tolerance_seconds")
    for value in (config["download"]["all_parts"], config["keep_intermediates"], *config["decode_verification"].values()):
        if type(value) is not bool:
            raise ValueError("开关必须为 YAML 布尔值")
    if not any(config["decode_verification"][key] for key in ("cpu", "gpu")):
        raise ValueError("至少启用一个完整解码设备")
    if config["reuse_check"] not in ("fast", "full"):
        raise ValueError("reuse_check 只能为 fast/full")
    positive(config["storage"]["replace_attempts"], "storage.replace_attempts", integer=True)
    positive(config["storage"]["retry_interval_seconds"], "storage.retry_interval_seconds")
    if type(config["logging"]["port"]) is not int or not 0 <= config["logging"]["port"] <= 65535:
        raise ValueError("日志端口无效")
    for key in ("state_file", "html_file"):
        name = config["logging"][key]
        if safe_name(name) != name or Path(name).name != name:
            raise ValueError("日志名称必须为单层文件名")
    for key, required, allowed in (("series_template", {"release_date", "title", "series_id"}, {"release_date", "title", "series_id"}),
                                    ("episode_template", {"index", "title", "quality", "audio"}, {"index", "title", "quality", "audio", "video_id"})):
        parsed = list(Formatter().parse(config["naming"][key]))
        fields = {f for _, f, _, _ in parsed if f is not None}
        if not required <= fields <= allowed or any(conversion for _, _, _, conversion in parsed):
            raise ValueError(f"naming.{key} 占位符无效")
    positive(config["naming"]["max_title_length"], "naming.max_title_length", integer=True)
    series_directory_name(config, "剧名", "series", "2000-01-01")
    for key in ("output_path", "work_dir"):
        if re.search(r"\s", str(config[key])):
            raise ValueError(f"{key} 禁止带空白")
    if config["output_path"].is_relative_to(config["work_dir"]) or config["work_dir"].is_relative_to(config["output_path"]):
        raise ValueError("成品目录与工作目录必须相互独立")
    profile["network"].update({key: config["network"][key] for key in ("proxy", "timeout_seconds", "max_response_bytes")})
    profile["network"]["referer"] = config["network"]["headers"]["Referer"]
    profile["media"]["user_agent"] = config["network"]["headers"]["User-Agent"]
    profile["media"].update(timeout_seconds=config["network"]["timeout_seconds"], proxy=config["network"]["proxy"])
    config["playback"] = profile
    return config
