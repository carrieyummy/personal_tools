"""FFmpeg 无损封装及 CPU/CUDA 完整解码；硬解错误不切换后端，子进程均支持取消和超时。"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from threading import Event
import time
from typing import Any

from .client import check_cancel
from .errors import ToolError


def process(command: list[str], log: Path, config: dict[str, Any], cancel: Event) -> None:
    """执行本地 command，stderr 写 log；config 控制超时/回收，cancel 为取消信号；成功无返回值。"""
    check_cancel(cancel)
    log.parent.mkdir(parents=True, exist_ok=True)
    child = None
    try:
        with log.open("wb") as output:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=output, cwd=log.parent.resolve(),
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            started = time.monotonic()
            while child.poll() is None:
                check_cancel(cancel)
                if time.monotonic() - started > config["verification"]["timeout_seconds"]:
                    raise ToolError(f"FFmpeg 超时，详情：{log}")
                cancel.wait(config["runtime"]["cancel_poll_interval_seconds"])
            check_cancel(cancel)
            if child.returncode:
                raise ToolError(f"FFmpeg 失败（{child.returncode}），详情：{log}")
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=config["runtime"]["process_terminate_timeout_seconds"])
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def base(config: dict[str, Any]) -> list[str]:
    """构造公共参数；config 指定 FFmpeg；返回禁止交互的命令前缀。"""
    return [str(config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-nostats"]


def gpu_probe(config: dict[str, Any], directory: Path, cancel: Event) -> None:
    """检查 CUDA 初始化；config 为配置，directory 保存日志，cancel 可取消；失败抛错，不变更开关。"""
    process(base(config) + ["-init_hw_device", "cuda=verification_gpu", "-f", "lavfi", "-i", "nullsrc=s=16x16:r=1",
                           "-frames:v", "1", "-f", "null", "-"], directory / "gpu_probe.log", config, cancel)


def mux(config: dict[str, Any], manifests: list[Path], output: Path, cancel: Event) -> None:
    """无损封装所有源音轨；config 提供程序，manifests 为本地清单，output 为候选 MP4，cancel 可取消；无返回值。

    分段 TS 的连续计数重置可能触发解复用警告，封装不将其直接判为码流损坏；后续必须严格完整解码及核对时长。
    """
    output.unlink(missing_ok=True)
    command = base(config) + ["-loglevel", "warning", "-n"]
    for manifest in manifests:
        command += ["-protocol_whitelist", "file,crypto,data", "-allowed_extensions", "ALL", "-allowed_segment_extensions", "ALL", "-extension_picky", "0", "-i", str(manifest)]
    command += ["-map", "0:v:0", "-map", "0:a?"]
    for index in range(1, len(manifests)):
        command += ["-map", f"{index}:a"]
    command += ["-c", "copy", "-movflags", "+faststart", str(output)]
    process(command, output.parent / "mux.log", config, cancel)


def probe(config: dict[str, Any], path: Path, cancel: Event, log_directory: Path | None = None) -> dict[str, Any]:
    """读取封装属性；config/path/cancel 为程序、文件和取消信号，log_directory 可隔离日志；返回媒体信息，缺关键值报错。"""
    log = (log_directory or path.parent) / "probe.log"
    process(base(config) + ["-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-t", "0", "-c", "copy", "-f", "null", "-"], log, config, cancel)
    header = log.read_text(encoding="utf-8", errors="replace").split("Stream mapping:")[0]
    video = [line for line in header.splitlines() if "Stream #" in line and "Video:" in line]
    audio = [line for line in header.splitlines() if "Stream #" in line and "Audio:" in line]
    duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", header)
    size = re.search(r"\b(\d+)x(\d+)(?=[, \[]|$)", video[0] if video else "")
    if len(video) != 1 or duration is None or size is None:
        raise ToolError("成品缺少有效视频轨、尺寸或时长")
    detail = video[0].split("Video:", 1)[1].strip()
    pixel = re.search(r"\b((?:yuv|gbr|rgb|nv|p0)[a-z0-9]+)(?:\(|,)", detail)
    colors = re.search(r"\b(bt2020nc|bt709|smpte170m)/(bt2020|bt709|smpte170m)/(smpte2084|arib-std-b67|bt709|smpte170m)\b", detail)
    return {"duration": int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3]),
            "width": int(size[1]), "height": int(size[2]), "video_codec": detail.split()[0].rstrip(","),
            "video_detail": detail, "pixel_format": pixel[1] if pixel else None,
            "color_space": colors[1] if colors else None, "color_primaries": colors[2] if colors else None,
            "color_transfer": colors[3] if colors else None,
            "audio_codecs": [line.split("Audio:", 1)[1].strip().split()[0].rstrip(",") for line in audio]}


def verify(config: dict[str, Any], path: Path, device: str, durations: list[float], source: dict[str, Any], cancel: Event,
           log_directory: Path | None = None) -> dict[str, Any]:
    """完整解码；config/path/device 指定文件及 cpu/gpu，durations 为所有源时长，source 为元数据，cancel 可取消，log_directory 可隔离日志；返回证据。"""
    if device not in ("cpu", "gpu"):
        raise ToolError("未知验证后端")
    directory = log_directory or path.parent
    directory.mkdir(parents=True, exist_ok=True)
    actual = probe(config, path, cancel, directory)
    tolerance = config["verification"]["duration_tolerance_seconds"]
    if any(abs(actual["duration"] - duration) > tolerance for duration in durations):
        raise ToolError("成品时长与完整源清单不一致")
    if actual["width"] != int(source["videoWidth"]) or actual["height"] != int(source["videoHeight"]):
        raise ToolError("成品尺寸与选定源不一致")
    if source.get("audioFormat") and not actual["audio_codecs"]:
        raise ToolError("成品缺少源音轨")
    if len(durations) > 1 and len(actual["audio_codecs"]) < len(durations) - 1:
        raise ToolError("成品未保留所有独立音轨")
    progress = directory / "decode.progress"
    progress.unlink(missing_ok=True)
    command = base(config) + ["-loglevel", "warning", "-xerror", "-err_detect", "explode"]
    if device == "gpu":
        command += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    command += ["-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-fps_mode", "passthrough", "-progress", str(progress), "-f", "null", "-"]
    started = time.monotonic()
    process(command, directory / f"decode_{device}.log", config, cancel)
    values = dict(line.split("=", 1) for line in progress.read_text(encoding="utf-8").splitlines() if "=" in line)
    if values.get("progress") != "end" or int(values.get("frame", 0)) <= 0 or int(values.get("dup_frames", 0)) or int(values.get("drop_frames", 0)):
        raise ToolError("完整解码未结束或发现丢帧/重复帧")
    decoded = int(values["out_time_us"]) / 1_000_000
    if abs(decoded - durations[0]) > tolerance:
        raise ToolError("完整解码时长与源清单不一致")
    return {"actual_media": actual, "device": device, "decoded_frames": int(values["frame"]), "decoded_seconds": decoded,
            "decode_seconds": round(time.monotonic() - started, 3), "exit_code": 0}
