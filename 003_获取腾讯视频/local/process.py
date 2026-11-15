"""执行媒体子进程，采样真实进度，并在超时或中断时回收进程。"""

from pathlib import Path
import re
import subprocess
import time
from threading import Event
from typing import Callable

from .runtime import check_cancel


def read_progress(path: Path) -> dict:
    """读取 path 中最后出现的 FFmpeg 进度值；文件尚未创建时返回空字典。"""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip()
    return values


def run_process(command: list[str], log_path: Path, config: dict,
                progress: Callable[[dict], None] | None = None, progress_path: Path | None = None,
                *, cancel_event: Event | None = None, cwd: Path | None = None, label: str = "FFmpeg") -> None:
    """执行 command 并将标准错误保存到新文件 log_path；config 控制超时/取消，
    progress 可消费 progress_path 的进度字典，cancel_event 为可选取消信号；cwd 为可选工作目录，label 为错误来源；成功无返回值，失败回收子进程。
    """
    process = None
    runtime = config["runtime"]
    try:
        check_cancel(cancel_event)
        with log_path.open("xb") as log:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log,
                                       stdin=subprocess.DEVNULL,
                                       cwd=str(cwd) if cwd is not None else None,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            started, sampled = time.monotonic(), float("-inf")
            while process.poll() is None:
                check_cancel(cancel_event)
                now = time.monotonic()
                if now - started > config["process_timeout_seconds"]:
                    raise TimeoutError(f"媒体处理超时，详情见 {log_path}")
                if progress and progress_path and now - sampled >= config["logging"]["progress_interval_seconds"]:
                    progress(read_progress(progress_path))
                    sampled = now
                time.sleep(runtime["cancel_poll_interval_seconds"])
            check_cancel(cancel_event)
            if progress and progress_path:
                progress(read_progress(progress_path))
            if process.returncode:
                raise RuntimeError(f"{label} 退出码 {process.returncode}，详情见 {log_path}")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=runtime["process_terminate_timeout_seconds"])
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def probe_media(config: dict, path: Path, log_path: Path, *, cancel_event: Event | None = None) -> dict:
    """按 config 探测 path 的视频/全部音轨属性并保存 log_path；cancel_event 可取消；返回媒体及缺失字段说明。"""
    run_process([str(config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-i", str(path),
                 "-map", "0:v:0?", "-map", "0:a:0?", "-t", "0", "-c", "copy", "-f", "null", "-"],
                log_path, config, cancel_event=cancel_event)
    header = log_path.read_text(encoding="utf-8", errors="replace").split("Stream mapping:")[0]
    video = next((line for line in header.splitlines() if "Stream #" in line and "Video:" in line), "")
    audio = [line for line in header.splitlines() if "Stream #" in line and "Audio:" in line]
    duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", header)
    size = re.search(r" (\d+)x(\d+)(?=[, \[]|$)", video)
    fps = re.search(r"\b([\d.]+) fps\b", video)
    colors = re.search(r"\b(bt2020nc|bt709|smpte170m)/(bt2020|bt709|smpte170m)/(smpte2084|arib-std-b67|bt709|smpte170m)\b", video)
    pixel = re.search(r"\b((?:yuv|gbr|rgb|nv|p0)[a-z0-9]+)(?:\(|,)", video)
    audio_streams = []
    for line in audio:
        rate = re.search(r"\b(\d+) Hz\b", line)
        bitrate = re.search(r"\b(\d+(?:\.\d+)?) kb/s\b", line)
        channels = re.search(r"\b(mono|stereo|\d+\.\d+(?:\([^)]*\))?|\d+ channels)\b", line)
        audio_streams.append({"codec": line.split("Audio:", 1)[1].strip().split()[0].rstrip(","),
                              "sample_rate": int(rate[1]) if rate else None,
                              "bitrate": float(bitrate[1]) * 1000 if bitrate else None,
                              "channels": channels[1] if channels else None})
    result = {
        "duration_seconds": (int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])) if duration else None,
        "width": int(size[1]) if size else None, "height": int(size[2]) if size else None,
        "fps": float(fps[1]) if fps else None,
        "video_codec": video.split("Video:", 1)[1].strip().split()[0].rstrip(",") if video else None,
        "audio_codecs": [line.split("Audio:", 1)[1].strip().split()[0].rstrip(",") for line in audio],
        "audio_streams": audio_streams,
        "pixel_format": pixel[1] if pixel else None,
        "color_space": colors[1] if colors else None,
        "color_primaries": colors[2] if colors else None,
        "color_transfer": colors[3] if colors else None,
    }
    result["errors"] = [f"文件未提供 {key}" for key, value in result.items() if value is None or value == []]
    return result
