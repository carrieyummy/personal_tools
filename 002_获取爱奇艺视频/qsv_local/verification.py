"""提供正式流程和性能比较共用的完整解码命令、结果校验及 CUDA 初始化检查。"""

from pathlib import Path
from threading import Event

from .process import run_process


def decode_command(config: dict, source: Path, progress: Path, device: str,
                   seconds: int | None = None) -> list[str]:
    """构造完整解码命令；config 提供 FFmpeg，source/progress 为媒体及进度路径，
    device 只接受 cpu/gpu，seconds 为可选的正预热时长；返回包含全部音轨和严格错误选项的参数列表。
    """
    if device not in ("cpu", "gpu"):
        raise ValueError(f"未知验证设备：{device}")
    command = [str(config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-nostats",
               "-loglevel", "warning", "-n", "-xerror", "-err_detect", "explode"]
    if device == "gpu":
        command += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    command += ["-i", str(source), "-map", "0:v:0", "-map", "0:a"]
    if seconds is not None:
        command += ["-t", str(seconds)]
    return command + ["-progress", str(progress), "-f", "null", "-"]


def validate_progress(progress: dict, expected_frames: int | None) -> dict:
    """校验 FFmpeg 最终 progress；expected_frames 为正预期帧数，None 仅用于短片预热；返回验证证明。

    本项目支持的输入每个视频 PES 对应一帧；所有设备均拒绝缺帧、重复帧和丢帧，不保证不同解码器对所有损坏码流报错等价。
    """
    frames = int(progress.get("frame", 0))
    if progress.get("progress") != "end" or frames <= 0:
        raise RuntimeError("FFmpeg 未完成视频解码")
    if expected_frames is not None and frames != expected_frames:
        raise RuntimeError("解码帧数与原始视频 PES 数不一致")
    if int(progress.get("dup_frames", 0)) or int(progress.get("drop_frames", 0)):
        raise RuntimeError("解码验证发现重复帧或丢帧")
    return {"frames": frames, "duration_seconds": int(progress["out_time_us"]) / 1000000,
            "decode_exit_code": 0}


def check_gpu(config: dict, work: Path, cancel_event: Event | None = None) -> None:
    """使用 config 指定的 FFmpeg 在 work 下检查真实 CUDA 上下文初始化；cancel_event 可取消检查，无返回值。

    仅检查设备及驱动初始化，具体视频格式的硬解能力仍由每个验证任务检查；失败不改用 CPU。
    """
    work.mkdir(parents=True, exist_ok=True)
    log = work / "gpu_probe.log"
    try:
        run_process([str(config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-loglevel", "warning",
                     "-init_hw_device", "cuda=verification_gpu", "-f", "lavfi", "-i", "nullsrc=s=16x16:r=1",
                     "-frames:v", "1", "-f", "null", "-"], log, config, cancel_event=cancel_event)
    except (RuntimeError, OSError, TimeoutError) as error:
        raise RuntimeError(f"已启用 GPU 验证，但 NVIDIA CUDA 初始化失败；不自动切换 CPU，详情见 {log}") from error
