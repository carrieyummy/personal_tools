"""读取 YAML、解析路径并校验剧集目录命名；不提供隐式机器路径或运行参数默认值。"""

import math
import os
from datetime import date
from pathlib import Path
import re
from string import Formatter

import yaml


def resolve_path(value: str, base: Path) -> Path:
    """展开字符串 value 的环境变量和用户目录，以 base 解析相对路径；返回绝对 Path。"""
    path = Path(os.path.expandvars(value)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def series_directory_name(config: dict, series: str, series_id: str, release_date: str) -> str:
    """按 config 的 naming.series_template 渲染目录名。

    series 为非空剧名，series_id 为非空字母数字串，release_date 为真实 ISO 日期；
    返回单层目录名，模板或日期无效时抛出 ValueError。
    """
    template = config["naming"]["series_template"]
    if not isinstance(template, str) or not template:
        raise ValueError("naming.series_template 必须是非空字符串")
    try:
        fields = list(Formatter().parse(template))
    except ValueError as error:
        raise ValueError("naming.series_template 的占位符格式无效") from error
    if ({field for _, field, _, _ in fields if field is not None} != {"title", "series_id", "release_date"}
            or any(spec or conversion for _, _, spec, conversion in fields)):
        raise ValueError("naming.series_template 必须且只能包含 {release_date}、{title} 和 {series_id}，不支持格式说明或转换")
    if not isinstance(series_id, str) or re.fullmatch(r"[A-Za-z0-9]+", series_id) is None:
        raise ValueError("下载记录中的整剧编号 vbarid 必须是非空字母数字串")
    if not isinstance(series, str) or not series.strip():
        raise ValueError("下载记录中的剧名不能为空")
    try:
        if not isinstance(release_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date):
            raise ValueError
        date.fromisoformat(release_date)
    except ValueError:
        raise ValueError("整剧首次上映日期必须是有效的 YYYY-MM-DD") from None
    name = template.format(title=series, series_id=series_id, release_date=release_date)
    if name in (".", "..") or name.endswith(".") or re.search(r'[\s<>:"/\\|?*\x00-\x1f]', name):
        raise ValueError("naming.series_template 生成的目录名含空白或非法路径字符")
    return name


def load_config(path: Path, *, source: str | None = None) -> dict:
    """读取主配置 path；source 供专项缓存验收显式指定来源，None 读取 YAML；返回已校验配置。"""
    path = path.expanduser().resolve()
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("主 YAML 必须是映射")
    config["config_path"] = path
    config["source"] = config["source"] if source is None else source
    if config["source"] not in ("download", "url"):
        raise ValueError("source 只允许 download 或 url")
    for key in ("input_root", "download_record", "output_path", "work_dir", "native_profile", "ffmpeg_path"):
        if key not in config and config["source"] == "url":
            continue
        config[key] = resolve_path(config[key], path.parent)
    if config["source"] == "download":
        profile_path = config["native_profile"]
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        profile["dll_root"] = resolve_path(profile["dll_root"], profile_path.parent)
        if not isinstance(profile.get("dll_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", profile["dll_sha256"]):
            raise ValueError("native_profile.dll_sha256 必须是 64 位小写十六进制 SHA-256")
        config["native"] = profile
        catalog_path = resolve_path(config["playback_profile"], path.parent)
        config["playback_profile"] = catalog_path
        config["playback"] = yaml.safe_load(catalog_path.read_text(encoding="utf-8-sig"))
    else:
        load_playback_config(config, path.parent)
    series_directory_name(config, "剧名", "seriesid", "2000-01-01")
    if config.get("reuse_check") not in ("fast", "full"):
        raise ValueError("reuse_check 只允许 fast 或 full")
    if config["source"] == "download":
        selection = config["selection"]
        for key in ("video_ids", "series_names"):
            if not isinstance(selection[key], list) or any(not isinstance(v, str) or not v.strip() for v in selection[key]):
                raise ValueError(f"selection.{key} 必须是非空字符串列表")
    devices = config["decode_verification"]
    if any(type(devices[key]) is not bool for key in ("cpu", "gpu")) or not (devices["cpu"] or devices["gpu"]):
        raise ValueError("decode_verification 必须设置 cpu/gpu 布尔值且至少启用一个")
    for key in ("copy_buffer_bytes", "process_timeout_seconds", "max_in_flight"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f"{key} 必须为正整数")
    if config["copy_buffer_bytes"] >= 2**31:
        raise ValueError("copy_buffer_bytes 超出本机接口有符号整数范围")
    tolerance = config["duration_tolerance_seconds"]
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("duration_tolerance_seconds 必须为有限正数")
    if type(config["keep_intermediates"]) is not bool:
        raise ValueError("keep_intermediates 必须为布尔值")
    for section, keys in (("logging", ("refresh_interval_seconds", "progress_interval_seconds", "final_state_timeout_seconds")),
                          ("runtime", ("cancel_poll_interval_seconds", "process_terminate_timeout_seconds"))):
        for key in keys:
            value = config[section][key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{section}.{key} 必须为有限正数")
    port = config["logging"]["port"]
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("logging.port 必须是 0 到 65535")
    for key in ("state_file", "html_file"):
        name = config["logging"][key]
        if not isinstance(name, str) or not name or name in (".", "..") or Path(name).name != name or any(c in name for c in '/\\'):
            raise ValueError(f"logging.{key} 必须是文件名")
    if config["source"] == "download" and not config["input_root"].is_dir():
        raise ValueError("input_root 不存在")
    for key in ("output_path", "work_dir"):
        if re.search(r"\s", str(config[key])):
            raise ValueError(f"{key} 不能包含空白")
        if config["source"] == "download" and config[key].is_relative_to(config["input_root"]):
            raise ValueError(f"{key} 必须位于客户端下载目录之外")
    if (config["output_path"].is_relative_to(config["work_dir"])
            or config["work_dir"].is_relative_to(config["output_path"])):
        raise ValueError("output_path 与 work_dir 必须相互独立")
    return config


def load_playback_config(config: dict, base: Path) -> None:
    """为 config 加载 URL/签名专项配置；base 为主配置目录；原地解析路径并校验联网及原生 ABI 边界，无返回值。"""
    for key in ("default_list", "playback_profile"):
        config[key] = resolve_path(config[key], base)
    profile_path = config["playback_profile"]
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8-sig"))
    signing_path = resolve_path(profile["playback"]["ckey_profile"], profile_path.parent)
    profile["playback"]["ckey"] = yaml.safe_load(signing_path.read_text(encoding="utf-8-sig"))
    signing = profile["playback"]["ckey"]
    if not re.fullmatch(r"[0-9a-f]{64}", str(signing["sha256"])):
        raise ValueError("签名专项配置 sha256 必须是 64 位小写十六进制摘要")
    for key in ("free_import_rva", "max_output_bytes", "session_seed_bits"):
        if type(signing[key]) is not int or signing[key] <= 0:
            raise ValueError(f"签名专项配置 {key} 必须为正整数")
    if not signing["process_names"] or not all(isinstance(value, str) and value for value in signing["process_names"]):
        raise ValueError("签名专项配置 process_names 必须包含播放器进程名")
    client_path = resolve_path(profile["native"]["client_profile"], profile_path.parent)
    client = yaml.safe_load(client_path.read_text(encoding="utf-8-sig"))
    profile["native"]["dll_root"] = resolve_path(client["dll_root"], client_path.parent)
    config["playback"] = profile
    tolerance = config["verification"]["fps_tolerance"]
    if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("verification.fps_tolerance 必须为有限正数")
    capacity = config["download"].get("queue_capacity_gb")
    if (type(capacity) not in (int, float) or capacity <= 0
            or (type(capacity) is float and (not math.isfinite(capacity) or not math.isfinite(capacity * 1024**3)))
            or capacity * 1024**3 < 1):
        raise ValueError("download.queue_capacity_gb 必须为至少 1 字节的有限正数（单位 GB）")
    for section, keys in (("download", ("quality", "threads", "refresh_attempts")), ("audio", ("quality",)),
                          ("authentication", ("refresh_interval_seconds", "recovery_attempts")),
                          ("network", ("timeout_seconds", "max_response_bytes"))):
        for key in keys:
            if type(config[section][key]) is not int or config[section][key] <= 0:
                raise ValueError(f"{section}.{key} 必须是正整数")
    for key in ("authentication_http_codes", "expired_http_codes"):
        codes = profile["playback"][key]
        if not isinstance(codes, list) or not codes or any(type(code) is not int or not 400 <= code <= 599 for code in codes):
            raise ValueError(f"播放专项配置 {key} 必须是非空 HTTP 错误码列表")
    for section, key in (("download", "retries"),):
        if type(config[section][key]) is not int or config[section][key] < 0:
            raise ValueError(f"{section}.{key} 必须是非负整数")
    if type(config["naming"]["max_title_length"]) is not int or config["naming"]["max_title_length"] <= 0:
        raise ValueError("naming.max_title_length 必须为正整数")
    for section, key in (("download", "all_parts"),):
        if type(config[section][key]) is not bool:
            raise ValueError(f"{section}.{key} 必须是布尔值")
    if not isinstance(config["network"]["proxy"], str) or not isinstance(config["network"]["headers"], dict):
        raise ValueError("network.proxy/headers 类型错误")
    template = config["naming"]["episode_template"]
    fields = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
    if not {"index", "title", "quality", "audio"}.issubset(fields) or not fields.issubset({"index", "title", "quality", "audio", "video_id"}):
        raise ValueError("naming.episode_template 须包含 index/title/quality/audio，且只能使用受支持的字段")
    rendered = template.format(index=1, title="标题", quality="画质", audio="音质", video_id="videoid")
    if re.search(r'[\s<>:"/\\|?*\x00-\x1f]', rendered) or rendered.endswith("."):
        raise ValueError("naming.episode_template 生成非法文件名")
