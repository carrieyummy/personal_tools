"""选择原始流、定位 FFmpeg 并使用流复制完成无损封装。"""

from __future__ import annotations

import subprocess
import math
from pathlib import Path
from typing import Any

from . import config as configuration
from .config import Settings
from .errors import ToolError
from .errors import check_cancel
from . import runtime


def size_number(value: Any) -> int | None:
    """将字节数 value 校验为正整数；拒绝布尔、负数、非有限数及非数值，未知返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value) or None


def format_size(value: Any) -> str:
    """将字节数 value 转为十进制 MB/GB 文本并保留两位小数；无有效大小时返回“未知”。"""
    size = size_number(value)
    if size is None:
        return '未知'
    divisor, unit = (1_000_000_000, 'GB') if size >= 1_000_000_000 else (1_000_000, 'MB')
    return f'{size / divisor:.2f} {unit}'


class StreamSizes:
    """按流维护完整大小；切换、续传和重试只修正对应流，不累计已传输字节或推测缺失长度。"""

    def __init__(self, formats: list[dict[str, Any]]) -> None:
        """从实际选中的 formats 读取 filesizes 和已有估算；只保留大小，无返回值。"""
        self.streams = []
        self.active: int | None = None
        for fmt in formats:
            exact = size_number(fmt.get('filesize'))
            approximate = size_number(fmt.get('filesize_approx'))
            self.streams.append({'bytes': exact if exact is not None else approximate,
                                 'estimated': exact is None and approximate is not None})

    def start(self, index: int) -> None:
        """将零基 index 指定为当前流；必须对应构造时的所选流，返回 None。"""
        if not 0 <= index < len(self.streams):
            raise ValueError('流大小索引越界')
        self.active = index

    def progress(self, status: dict[str, Any]) -> None:
        """从当前流的 status 回调修正完整长度；finished 的完整 downloaded_bytes 可确认长度，无返回值。"""
        if self.active is None:
            return
        exact = size_number(status.get('downloaded_bytes')) if status.get('status') == 'finished' else None
        exact = exact if exact is not None else size_number(status.get('total_bytes'))
        approximate = size_number(status.get('total_bytes_estimate'))
        stream = self.streams[self.active]
        if exact is not None:
            stream.update(bytes=exact, estimated=False)
        elif approximate is not None and (stream['bytes'] is None or stream['estimated']):
            stream.update(bytes=approximate, estimated=True)

    def expected(self) -> dict[str, Any]:
        """返回整文件预计大小及估算标记；任意流长度未知时整文件未知，无参数。"""
        known = bool(self.streams) and all(stream['bytes'] is not None for stream in self.streams)
        return {'expected_size_bytes': sum(stream['bytes'] for stream in self.streams) if known else None,
                'expected_size_estimated': known and any(stream['estimated'] for stream in self.streams)}


def expected_size(formats: list[dict[str, Any]]) -> dict[str, Any]:
    """计算所选 formats 的完整预计大小；任意流未知则返回未知，沿用 StreamSizes 的估算规则。"""
    return StreamSizes(formats).expected()


def file_size_fields(entry: dict[str, Any]) -> dict[str, str]:
    """为日志 entry 生成预计、实际和列表大小文本；实际大小优先，旧记录缺字段时显示未知。"""
    expected, actual = size_number(entry.get('expected_size_bytes')), size_number(entry.get('actual_size_bytes'))
    expected_text = ('约 ' if entry.get('expected_size_estimated') else '') + format_size(expected) if expected is not None else '未知'
    display = '未知'
    if actual is not None:
        display = '实际 ' + format_size(actual)
    elif expected is not None:
        display = '预计 ' + expected_text
    return {'expected_size_text': expected_text, 'actual_size_text': format_size(actual), 'file_size_text': display}


def size_priority(entry: dict[str, Any]) -> tuple[bool, bool, bool]:
    """返回 entry 大小信息的优先级：实际大小、已知预计大小、准确流长度依次优先；供重复版本共享。"""
    return (size_number(entry.get('actual_size_bytes')) is not None,
            size_number(entry.get('expected_size_bytes')) is not None, not entry.get('expected_size_estimated', False))


def summarize_sizes(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总 entries 中的下载版本；按 download_key 去重，含复用，排除合并，未知不计为零，返回结构及文本。"""
    unique: dict[Any, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if entry.get('kind') != '下载':
            continue
        key = entry.get('download_key') or ('entry', entry.get('id', index))
        if isinstance(key, list):
            key = tuple(key)
        if key not in unique or size_priority(entry) > size_priority(unique[key]):
            unique[key] = entry
    total, unknown, pending, estimated = 0, 0, False, False
    for entry in unique.values():
        actual = size_number(entry.get('actual_size_bytes'))
        expected = size_number(entry.get('expected_size_bytes'))
        value = actual if actual is not None else expected
        if value is None:
            unknown += 1
        else:
            total += value
            pending |= actual is None
            estimated |= actual is None and bool(entry.get('expected_size_estimated'))
    known = len(unique) - unknown
    amount = ('约 ' if estimated else '') + format_size(total)
    text = (('已知合计 ' if unknown else '预计合计 ' if pending else '合计 ') + amount
            if known else '未知' if unique else '无下载文件')
    if unknown:
        text += f'，另 {unknown} 个文件大小未知' if known else f'（{unknown} 个文件）'
    return {'known_bytes': total, 'file_count': len(unique), 'unknown_count': unknown,
            'has_expected': pending, 'estimated': estimated, 'text': text}


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
    """用 executable 原样合并 sources/formats 至 folder/stem；MP4 失败改试 MKV，返回成功文件路径，取消保留源流。"""
    preferred = choose_container(formats)
    errors = []
    for extension in (["mp4", "mkv"] if preferred == "mp4" else ["mkv"]):
        destination = folder / f"{stem}.{extension}"
        temporary = folder / f"{stem}.partial.{extension}"
        command = ffmpeg_command(executable, sources, formats, temporary)
        try:
            result = runtime.run_process(command, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            check_cancel()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
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
        executable = configuration.expand_path(settings.ffmpeg, config_dir)
    if not executable.is_file():
        raise ToolError(f"FFmpeg 不存在：{executable}；请先运行“准备环境”")
    return str(executable)
