"""读取链接下载及播放缓存适配配置，所有运行参数取自 YAML。"""

import math
import json
from copy import deepcopy
from pathlib import Path
from string import Formatter
import yaml

from ..configuration import resolve_path
from ..storage import valid_digest
from .albums import AlbumMetadataCache


def read_profile(path: Path) -> dict:
    """读取专项配置 path，解析路径及客户端安装目录并校验边界；不依赖离线下载目录、数据库或实际缓存文件。"""
    path = path.expanduser().resolve()
    profile = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(profile, dict):
        raise ValueError("研究配置必须为 YAML 对象")
    for key in ("main_config", "download_list", "cache_root", "report_dir"):
        profile[key] = resolve_path(profile[key], path.parent)
    main = yaml.safe_load(profile["main_config"].read_text(encoding="utf-8-sig"))
    # 命名设置随本次运行固定；运行中编辑配置不能将同一批次的剧集拆到不同日期目录。
    if "naming" in main:
        profile["_naming_snapshot"] = deepcopy(main["naming"])
    profile["_album_cache"] = AlbumMetadataCache()
    native_path = resolve_path(main["native_profile"], profile["main_config"].parent)
    client = yaml.safe_load(native_path.read_text(encoding="utf-8-sig"))
    profile["client_root"] = resolve_path(client["dll_root"], native_path.parent)
    for section, names in (("network", ("timeout_seconds", "max_response_bytes")),
                           ("native", ("scan_max_region_bytes", "scan_max_total_bytes", "max_rows", "max_text_bytes"))):
        for name in names:
            value = profile[section][name]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{section}.{name} 必须为大于零的有限数字")
            if name != "timeout_seconds" and type(value) is not int:
                raise ValueError(f"{section}.{name} 必须为正整数")
    digest = profile["native"]["sha256"]
    if not valid_digest(digest):
        raise ValueError("研究模块 SHA-256 配置无效")
    playback = profile["playback"]
    for name in ("capture_seconds", "followup_seconds", "cleanup_timeout_seconds", "cleanup_poll_seconds",
                 "max_requests", "max_body_bytes", "max_text_bytes", "max_headers"):
        value = playback[name]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"playback.{name} 必须为大于零的有限数字")
        if name in ("max_requests", "max_body_bytes", "max_text_bytes", "max_headers") and type(value) is not int:
            raise ValueError(f"playback.{name} 必须为正整数")
    for key in ("qtp", "player"):
        digest = playback[key]["sha256"]
        if not valid_digest(digest):
            raise ValueError(f"playback.{key}.sha256 无效")
    positions = playback["media_probe"]["positions"]
    if not isinstance(positions, list) or not positions or any(
            type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in positions):
        raise ValueError("媒体采样位置必须为 [0, 1] 范围内的有限数字列表")
    maximum = playback["media_probe"]["max_segment_bytes"]
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("媒体分段字节上限必须为正整数")
    for key, minimum in (("bitstream", 1), ("hdr_type", -1), ("frame_rate", 0), ("movie_type", 0),
                         ("audio_language", 0), ("audio_type", 0), ("audio_channel_type", 0)):
        if type(playback["request"][key]) is not int or playback["request"][key] < minimum:
            raise ValueError(f"playback.request.{key} 超出参数约束")
    if (not isinstance(playback["request"]["extra"], dict) or not isinstance(playback["request"]["vrs_param"], str)
            or not isinstance(playback["request"]["bitstream_extend_info"], dict) or not isinstance(playback["request"]["audio_extend_info"], dict)):
        raise ValueError("播放请求 extra 必须是对象，vrs_param 必须是字符串")
    selection = playback["selection"]
    if type(selection["prefer_native_selected"]) is not bool:
        raise ValueError("selection.prefer_native_selected 必须为布尔值")
    audio = playback["audio_selection"]
    if not isinstance(audio["rank_fields"], list) or not audio["rank_fields"] or len(set(audio["rank_fields"])) != len(audio["rank_fields"]) or not set(audio["rank_fields"]) <= {"bid", "ct"}:
        raise ValueError("音轨档位字段必须为不重复的原生 bid/ct 字段")
    if not isinstance(audio["codec_types"], dict) or not audio["codec_types"] or any(type(value) is not int or value <= 0 for value in audio["codec_types"].values()):
        raise ValueError("音频编码枚举必须为正整数映射")
    if set(audio["output_codecs"]) != set(audio["codec_types"]) or any(not isinstance(values, list) or not values or any(not isinstance(value, str) or not value for value in values) for values in audio["output_codecs"].values()):
        raise ValueError("每种原生音频编码必须配置有效的成品编码列表")
    suffixes = playback["cdn_host_suffixes"]
    if not isinstance(suffixes, list) or not suffixes or any(not isinstance(value, str) or not value.startswith(".") or "/" in value for value in suffixes):
        raise ValueError("媒体 CDN 后缀必须是非空域名后缀列表")
    fields = selection["rank_fields"]
    if not isinstance(fields, list) or not fields or len(set(fields)) != len(fields) or not set(fields) <= {"bid", "enhanced", "br", "fr"}:
        raise ValueError("画质分组字段必须为不重复的有效字段列表")
    for name in ("hdr_preference", "codec_preference", "supported_codecs"):
        values = selection[name]
        if not isinstance(values, list) or not values or any(type(value) is not int for value in values) or len(set(values)) != len(values):
            raise ValueError(f"selection.{name} 必须为不重复的整数列表")
    if (not isinstance(selection["supported_formats"], list) or not selection["supported_formats"]
            or any(not isinstance(value, str) or not value for value in selection["supported_formats"])):
        raise ValueError("支持的媒体格式列表无效")
    if type(selection["sdr_request_hdr"]) is not int or selection["sdr_request_hdr"] < 0:
        raise ValueError("SDR 请求 HDR 类型必须为非负整数")
    ranks = selection["supported_ranks"]
    if not isinstance(ranks, list) or not ranks or any(type(value) is not int or value <= 0 for value in ranks) or len(set(ranks)) != len(ranks):
        raise ValueError("已验证的画质排名必须为不重复的正整数列表")
    for name in ("episode_page_size", "max_episode_pages"):
        if type(profile["network"][name]) is not int or profile["network"][name] <= 0:
            raise ValueError(f"network.{name} 必须为正整数")
    offset = profile["network"]["album_date_utc_offset_minutes"]
    if type(offset) is not int or not -1440 < offset < 1440:
        raise ValueError("network.album_date_utc_offset_minutes 必须为绝对值小于 1440 的整数分钟")
    album_url = profile["network"]["album_info_url"]
    if not isinstance(album_url, str) or {field for _, field, _, _ in Formatter().parse(album_url) if field is not None} != {"album_id"}:
        raise ValueError("network.album_info_url 必须包含且仅包含 album_id 字段")
    for name in ("xor_mask", "legacy_threshold", "legacy_offset", "legacy_multiplier"):
        if type(profile["network"]["play_url_id"][name]) is not int or profile["network"]["play_url_id"][name] <= 0:
            raise ValueError(f"network.play_url_id.{name} 必须为正整数")
    programs = playback["programs"]
    if type(programs["input_size"]) is not int or programs["input_size"] <= 0:
        raise ValueError("原生播放输入长度无效")
    required = {"bitstream", "hdr_type", "frame_rate", "bitstream_extend_info", "movie_type", "tvid", "vid", "charge", "vrs_param", "extra", "audio_language", "audio_type", "audio_channel_type", "audio_extend_info"}
    if set(programs["fields"]) != required or set(programs["capacities"]) != {"tvid", "vid", "bitstream_extend_info", "audio_extend_info"}:
        raise ValueError("原生播放输入字段或容量定义不完整")
    occupied = []
    for key, offset in programs["fields"].items():
        size = programs["capacities"].get(key, 8 if key in ("extra", "vrs_param") else 1 if key == "charge" else 4)
        if type(offset) is not int or offset < 0 or type(size) is not int or size <= 0 or offset + size > programs["input_size"]:
            raise ValueError("原生播放输入字段超出已配置结构范围")
        occupied.append((offset, offset + size))
    occupied.sort()
    if any(left[1] > right[0] for left, right in zip(occupied, occupied[1:])):
        raise ValueError("原生播放输入字段相互重叠")
    if playback["max_body_bytes"] >= 2 ** 31:
        raise ValueError("原生状态缓冲上限必须小于 2 GiB")
    try:
        for name in ("extra", "bitstream_extend_info", "audio_extend_info"):
            json.dumps(playback["request"][name], allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("播放请求扩展参数必须为可编码的有限 JSON 对象") from None
    if type(profile["network"]["use_environment_proxy"]) is not bool:
        raise ValueError("network.use_environment_proxy 必须是布尔值")
    for name in ("workers", "attempts", "retry_seconds", "duration_tolerance_seconds", "progress_seconds", "max_fragment_bytes"):
        value = playback["media_download"][name]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"playback.media_download.{name} 必须为大于零的有限数字")
        if name in ("workers", "attempts", "max_fragment_bytes") and type(value) is not int:
            raise ValueError(f"playback.media_download.{name} 必须为正整数")
    return profile

