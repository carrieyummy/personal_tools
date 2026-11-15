"""选择原始流、定位 FFmpeg 并使用流复制完成无损封装。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import config as configuration
from .config import Settings
from .errors import ToolError
from .errors import check_cancel
from . import runtime


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
