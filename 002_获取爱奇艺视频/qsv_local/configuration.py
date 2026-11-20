"""读取 YAML 配置并统一解析路径；不复制配置中的运行参数默认值。"""

from pathlib import Path
from string import Formatter
import math
import codecs

import yaml

from .naming import validate_component
from .storage import validate_lock_file


def validate_concurrency(config: dict) -> None:
    """校验 config 的下载线程、等待容量和后处理上限；配置无效时抛出 ValueError，无返回值。"""
    download = config.get("download")
    if not isinstance(download, dict):
        raise ValueError("download 必须为 YAML 对象")
    for name, value in (("download.threads", download.get("threads")),
                        ("max_in_flight", config.get("max_in_flight"))):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} 必须为正整数")
    capacity = download.get("queue_capacity_gb")
    if (type(capacity) not in (int, float) or capacity < 1 / 1024**3
            or capacity > (2**63 - 1) / 1024**3 or not math.isfinite(capacity)):
        raise ValueError("download.queue_capacity_gb 必须为可表示至少一字节的大于零的有限数字")


def read_config(path: Path) -> dict:
    """读取任务配置；path 为 YAML 文件；返回已校验参数及绝对 Path 对象。"""
    path = path.expanduser().resolve()
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("配置必须是 YAML 对象")
    validate_lock_file(config["logging"]["lock_file"])
    validate_concurrency(config)
    for name in ("input_root", "output_path", "work_dir", "native_profile", "ffmpeg_path"):
        config[name] = resolve_path(config[name], path.parent)
    if not config["input_root"].is_dir():
        raise ValueError("input_root 必须是已存在的目录")
    selections = config["input_path"]
    if not isinstance(selections, list):
        raise ValueError("input_path 必须为目录列表；扫描全部时填写 []")
    config["input_path"] = [resolve_path(value, config["input_root"]) for value in selections]
    for selected in config["input_path"]:
        if not selected.is_relative_to(config["input_root"]) or not selected.is_dir():
            raise ValueError(f"input_path 必须指向 input_root 内已存在的目录：{selected}")
    for name in ("output_path", "work_dir"):
        if config[name].exists() and not config[name].is_dir():
            raise ValueError(f"{name} 必须是目录：{config[name]}")
    for name in ("copy_buffer_bytes", "max_pes_bytes", "process_timeout_seconds", "max_in_flight"):
        value = config[name]
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} 必须为正整数")
    if type(config["keep_intermediates"]) is not bool:
        raise ValueError("keep_intermediates 必须为布尔值")
    devices = config.get("decode_verification")
    if not isinstance(devices, dict) or any(type(devices.get(name)) is not bool for name in ("cpu", "gpu")):
        raise ValueError("decode_verification 必须包含 cpu、gpu 两个布尔开关")
    if not (devices["cpu"] or devices["gpu"]):
        raise ValueError("decode_verification.cpu/gpu 至少开启一个")
    naming = config.get("naming")
    if not isinstance(naming, dict):
        raise ValueError("naming 必须为 YAML 对象")
    from .playback.naming import movie_name
    movie_name(naming, {"release_date": "2000-01-02", "title": "影片", "album_id": "123"}, "画质")
    for key, expected in (("series_template", {"title", "series_id"}), ("episode_template", {"index", "title"})):
        template = naming.get(key)
        if not isinstance(template, str) or not template:
            raise ValueError(f"naming.{key} 必须为非空字符串")
        fields = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
        if fields != expected:
            raise ValueError(f"naming.{key} 必须包含且仅包含：{', '.join(sorted(expected))}")
        try:
            sample = template.format(title="标题", series_id="123", index=1)
        except (ValueError, KeyError, IndexError) as error:
            raise ValueError(f"naming.{key} 模板格式无效") from error
        validate_component(sample)
    guard = config["download_guard"]
    if not isinstance(guard, dict):
        raise ValueError("download_guard 必须为 YAML 对象")
    guard["database_path"] = resolve_path(guard["database_path"], path.parent)
    if not guard["database_path"].is_file():
        raise ValueError("download_guard.database_path 必须是已有的客户端下载数据库；禁止在状态不明时读取视频")
    for name in ("completed_record", "active_record", "xml_encoding"):
        if not isinstance(guard[name], str) or not guard[name].strip():
            raise ValueError(f"download_guard.{name} 必须为非空字符串")
    if guard["completed_record"].casefold() == guard["active_record"].casefold():
        raise ValueError("download_guard 的完成和进行中记录名必须不同")
    codecs.lookup(guard["xml_encoding"])
    states = guard["completed_states"]
    if not isinstance(states, list) or not states or any(type(value) is not int for value in states):
        raise ValueError("download_guard.completed_states 必须为非空整数列表")
    suffixes = guard["temporary_suffixes"]
    if (not isinstance(suffixes, list) or not suffixes
            or any(not isinstance(value, str) or not value.startswith(".") or len(value) < 2
                   or "/" in value or "\\" in value for value in suffixes)):
        raise ValueError("download_guard.temporary_suffixes 必须为非空的下载临时后缀列表")
    timeout = guard["database_timeout_seconds"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("download_guard.database_timeout_seconds 必须为大于零的有限数字")
    logging = config["logging"]
    if not isinstance(logging, dict):
        raise ValueError("logging 必须为 YAML 对象")
    for name in ("state_file", "html_file"):
        value = logging[name]
        if (not isinstance(value, str) or not value.strip() or Path(value).name != value
                or Path(value).drive or value in (".", "..")):
            raise ValueError(f"logging.{name} 必须是输出根目录内的文件名")
        if not (config["output_path"] / value).resolve().is_relative_to(config["output_path"]):
            raise ValueError(f"logging.{name} 链接超出输出根目录")
    if logging["state_file"].casefold() == "video_info.json":
        raise ValueError("logging.state_file 不能与目录成品索引 video_info.json 同名")
    if Path(logging["state_file"]).suffix.lower() != ".json":
        raise ValueError("logging.state_file 必须使用 .json 扩展名")
    if Path(logging["html_file"]).suffix.lower() != ".html":
        raise ValueError("logging.html_file 必须使用 .html 扩展名")
    if type(logging["port"]) is not int or not 0 <= logging["port"] <= 65535:
        raise ValueError("logging.port 必须是 0～65535 的整数")
    if type(logging["write_retries"]) is not int or logging["write_retries"] < 0:
        raise ValueError("logging.write_retries 必须是非负整数")
    for section, names in (
        ("logging", ("refresh_interval_seconds", "progress_interval_seconds", "write_retry_interval_seconds")),
        ("runtime", ("cancel_poll_interval_seconds", "process_terminate_timeout_seconds")),
    ):
        if not isinstance(config[section], dict):
            raise ValueError(f"{section} 必须为 YAML 对象")
        for name in names:
            value = config[section][name]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{section}.{name} 必须为大于零的有限数字")
    profile_path = config["native_profile"]
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    if not isinstance(profile, dict):
        raise ValueError("native_profile 必须为 YAML 对象")
    for name in ("dll_root", "cert_path"):
        profile[name] = resolve_path(profile[name], profile_path.parent)
    config["sdk"] = profile
    return config


def resolve_path(value: str, base: Path) -> Path:
    """解析路径；value 为非空路径字符串，base 为相对路径基准；返回绝对路径，不隐式展开环境变量。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("路径配置必须是非空字符串")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()
