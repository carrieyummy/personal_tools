"""读取用户可编辑 YAML，集中解析路径与运行参数，不在代码中复制配置默认值。"""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
from string import Template
from string import Formatter
from typing import Any

import yaml

from .errors import ToolError


def expand_path(value: str, base: Path) -> Path:
    """展开路径；value 支持 ~、%变量% 和 ${变量}，base 是相对路径基准；返回绝对 Path。"""
    if not isinstance(value, str) or not value.strip():
        raise ToolError("路径必须是非空字符串")
    try:
        expanded = re.sub(r"%([^%]+)%", lambda match: os.environ[match.group(1)], value)
        expanded = Template(expanded).substitute(os.environ)
    except (KeyError, ValueError) as error:
        raise ToolError("路径包含未定义或格式错误的环境变量") from error
    path = Path(expanded).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def read_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML 映射；path 为配置或清单文件；返回字典，损坏或根类型错误时抛出 ToolError。"""
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError) as error:
        raise ToolError(f"无法读取 YAML：{path}") from error
    if not isinstance(value, dict):
        raise ToolError(f"YAML 根节点必须为映射：{path}")
    return value


def positive(value: Any, name: str, *, integer: bool = False, zero: bool = False) -> None:
    """校验数值；value 为配置值，name 为字段名，integer/zero 控制整数及零值；无返回值。"""
    valid = type(value) is int if integer else type(value) in (int, float)
    if not valid or not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise ToolError(f"{name} 必须为{'非负' if zero else '正'}{'整数' if integer else '有限数字'}")


def filename(value: Any, name: str, suffix: str | None = None) -> None:
    """校验文件名；value 不得含目录或空白，name 是字段名，suffix 是可选扩展名；无返回值。"""
    if not isinstance(value, str) or not value or Path(value).name != value or Path(value).drive or value in (".", "..") or re.search(r"\s", value):
        raise ToolError(f"{name} 必须是不含目录和空白的文件名")
    if suffix and Path(value).suffix.lower() != suffix:
        raise ToolError(f"{name} 必须使用 {suffix} 扩展名")


def read_config(path: Path) -> dict[str, Any]:
    """加载正式主配置；path 相对当前工作目录；返回已校验字典及绝对路径对象。"""
    path = path.expanduser().resolve()
    config = read_yaml(path)
    config["config_path"] = path
    for key in ("output_path", "work_dir", "ffmpeg_path", "native_profile", "default_list"):
        config[key] = expand_path(config[key], path.parent)
    for key in ("output_path", "work_dir"):
        target = config[key]
        if re.search(r"\s", str(target)) or (target.exists() and not target.is_dir()):
            raise ToolError(f"{key} 必须是不含空白的目录")
    if config["output_path"] == config["work_dir"] or config["output_path"].is_relative_to(config["work_dir"]) or config["work_dir"].is_relative_to(config["output_path"]):
        raise ToolError("输出目录和工作目录必须相互独立")
    if not config["ffmpeg_path"].is_file():
        raise ToolError("ffmpeg_path 必须指向已有 FFmpeg 可执行文件")
    profile_path = config["native_profile"]
    config["profile"] = read_yaml(profile_path)
    config["auth"]["user_data"] = expand_path(config["auth"]["user_data"], path.parent)
    for section, fields in {
        "download": ("threads", "buffer_bytes", "refresh_attempts"),
        "pipeline": ("mux_slots", "publish_slots"),
        "verification": ("timeout_seconds",),
        "runtime": ("cancel_poll_interval_seconds", "process_terminate_timeout_seconds"),
        "network": ("timeout_seconds", "max_json_bytes"),
        "logging": ("refresh_interval_seconds", "progress_interval_seconds", "write_retry_interval_seconds"),
    }.items():
        for field in fields:
            positive(config[section][field], f"{section}.{field}", integer=field in ("threads", "buffer_bytes", "refresh_attempts", "mux_slots", "publish_slots", "max_in_flight", "max_json_bytes"))
    try:
        process_limit = config["max_in_flight"]
    except KeyError as error:
        raise ToolError("缺少顶层 max_in_flight 配置") from error
    positive(process_limit, "max_in_flight", integer=True)
    for section, field in (("download", "retries"), ("logging", "write_retries")):
        positive(config[section][field], f"{section}.{field}", integer=True, zero=True)
    try:
        capacity = config["download"]["queue_capacity_gb"]
    except KeyError as error:
        raise ToolError("缺少 download.queue_capacity_gb 配置") from error
    positive(capacity, "download.queue_capacity_gb")
    if capacity < 1 / 1024**3 or not math.isfinite(capacity * 1024**3):
        raise ToolError("download.queue_capacity_gb 必须为有限正数且至少为 1 字节")
    positive(config["download"]["quality"], "download.quality", integer=True)
    positive(config["verification"]["duration_tolerance_seconds"], "verification.duration_tolerance_seconds", zero=True)
    positive(config["naming"]["max_title_length"], "naming.max_title_length", integer=True)
    for section, field in (("download", "all_parts"), ("download", "keep_intermediates"), ("decode_verification", "cpu"), ("decode_verification", "gpu")):
        if type(config[section][field]) is not bool:
            raise ToolError(f"{section}.{field} 必须为布尔值")
    if set(config["decode_verification"]) != {"cpu", "gpu"}:
        raise ToolError("decode_verification 只接受 cpu 和 gpu 两个开关")
    if not any(config["decode_verification"].values()):
        raise ToolError("CPU/GPU 完整解码验证至少启用一个")
    for key, suffix in (("state_file", ".json"), ("html_file", ".html"), ("index_file", ".json"), ("lock_file", None), ("work_lock_file", None)):
        filename(config["logging"][key], f"logging.{key}", suffix)
        if not config["logging"][key].startswith(("mgtv_", ".mgtv_")):
            raise ToolError(f"logging.{key} 必须保留 mgtv_ 前缀")
    if len(set(config["logging"][key] for key in ("state_file", "index_file", "lock_file", "work_lock_file"))) != 4:
        raise ToolError("日志、索引和锁文件名不能重复")
    port = config["logging"]["port"]
    if type(port) is not int or not 0 <= port <= 65535:
        raise ToolError("logging.port 必须为 0～65535 的整数")
    if not isinstance(config["network"]["proxy"], str) or not isinstance(config["network"]["headers"], dict):
        raise ToolError("network.proxy/headers 类型错误")
    profile = config["profile"]
    for section, key in (("playback", "endpoint"), ("catalog", "endpoint")):
        if not str(profile[section][key]).startswith("https://"):
            raise ToolError(f"{section}.endpoint 必须使用 HTTPS")
    from urllib.parse import urlsplit
    metadata = profile["series_metadata"]
    address = metadata["page_url"]
    if not isinstance(address, str) or {field for _, field, _, _ in Formatter().parse(address) if field is not None} != {"clip_id"}:
        raise ToolError("series_metadata.page_url 必须包含唯一的 clip_id 模板字段")
    parsed = urlsplit(address.format_map({"clip_id": "1"}))
    if parsed.scheme != "https" or not parsed.hostname or not (parsed.hostname == "mgtv.com" or parsed.hostname.endswith(".mgtv.com")) or parsed.query or parsed.fragment:
        raise ToolError("series_metadata.page_url 必须为芒果官方 HTTPS 专辑页面")
    if not isinstance(metadata["release_date_field"], str) or not re.fullmatch(r"[A-Za-z_$][\w$]*", metadata["release_date_field"]):
        raise ToolError("series_metadata.release_date_field 必须是有效字段名")
    positive(metadata["max_page_bytes"], "series_metadata.max_page_bytes", integer=True)
    prefix = config["naming"]["series_date_prefix"]
    if not isinstance(prefix, str):
        raise ToolError("naming.series_date_prefix 必须是字符串")
    parsed_prefix = list(Formatter().parse(prefix))
    if [field for _, field, _, _ in parsed_prefix if field is not None] != ["release_date"] or any(spec or conversion for _, field, spec, conversion in parsed_prefix if field is not None):
        raise ToolError("naming.series_date_prefix 必须且只能包含一次 release_date")
    rendered_prefix = prefix.format_map({"release_date": "2000-01-02"})
    if re.search(r'[<>:"/\\|?*\s]', rendered_prefix):
        raise ToolError("上映日期前缀不能包含路径分隔符、空白或 Windows 非法字符")
    positive(config["naming"]["max_filename_length"], "naming.max_filename_length", integer=True)
    if config["naming"]["max_filename_length"] > 240:
        raise ToolError("max_filename_length 不能超过 Windows 单段文件名上限预留值 240")
    for key, required, allowed in (("series_template", {"title", "clip_id"}, {"title", "clip_id"}),
                                   ("episode_template", {"index", "title", "quality"}, {"index", "title", "quality", "video_id", "clip_id"})):
        fields = {field for _, field, _, _ in Formatter().parse(config["naming"][key]) if field is not None}
        if not required.issubset(fields) or not fields.issubset(allowed):
            raise ToolError(f"naming.{key} 缺少必要字段或包含未知字段")
        rendered = config["naming"][key].format_map({"title": "标题", "clip_id": "1", "video_id": "2", "index": 1, "quality": "画质"})
        if re.search(r'[<>:"/\\|?*\s]', rendered):
            raise ToolError(f"naming.{key} 不能生成目录、空白或 Windows 非法字符")
    if not isinstance(profile["playback"]["selection_parameter"], str):
        raise ToolError("播放档位参数名必须为字符串")
    if profile["dispatch"]["scheme"] != "https" or not profile["dispatch"]["success_states"]:
        raise ToolError("源调度必须配置 HTTPS 及非空成功状态列表")
    return config
