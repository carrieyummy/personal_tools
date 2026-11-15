"""分阶段准备和验证候选 MP4；成品发布、索引提交及成功清理由批次调度器负责。"""

from pathlib import Path
import sys
from threading import Event
import time
from typing import Callable

from .container import QsvFile
from .download_state import DownloadNotReady, require_completed
from .native import NativeV5
from .process import probe_media, read_progress, run_process
from .runtime import TaskCancelled, check_cancel
from .storage import atomic_json, fingerprint
from .transport import TransportDecoder
from .verification import decode_command, validate_progress


def error_status(error: BaseException) -> str:
    """按 error 区分中断、下载等待或失败；返回现场报告状态，不抑制异常。"""
    return ("interrupted" if isinstance(error, (KeyboardInterrupt, TaskCancelled))
            else "waiting" if isinstance(error, DownloadNotReady) else "failed")


def save_report(report: dict, work: Path, active_error: BaseException | None = None) -> None:
    """把 report 原子保存到 work；active_error 非空时仅提示写盘错误，避免掩盖原始异常，无返回值。"""
    try:
        atomic_json(work / "report.json", report)
    except OSError as error:
        if active_error is None:
            raise
        print(f"失败现场报告无法保存：{error}", file=sys.stderr, flush=True)


def prepare(config: dict, source: Path, work: Path, notify: Callable,
            cancel_event: Event | None = None) -> dict:
    """按 config 将 source 准备到独占目录 work，notify 接收阶段进度，cancel_event 可取消；
    返回含来源指纹和已提取媒体的报告。调用者须保证同批次仅一个准备任务持有 SDK。
    """
    check_cancel(cancel_event)
    work.mkdir(parents=True, exist_ok=False)
    started, active_error = time.monotonic(), None
    candidate = work / "validated.partial.mp4"
    report = {"status": "preparing", "source": str(source), "candidate": str(candidate), "work_dir": str(work)}

    def advance(stage: str, **fields) -> None:
        """检查取消后转发 stage 和 fields；作为提取及 TS 块处理的取消检查点，无返回值。"""
        check_cancel(cancel_event)
        notify(stage, **fields)

    try:
        advance("reading", detail="读取源文件与授权信息")
        require_completed(config, source)
        original = fingerprint(source, config["copy_buffer_bytes"], force=True, cancel_event=cancel_event)
        report.update(source_sha256=original["sha256"], source_fingerprint=original)
        require_completed(config, source)
        qsv = QsvFile(source)
        report["license_version"] = str(qsv.info["license_version"])
        report["format"] = {"container_version": qsv.container_version,
                            "license_version": report["license_version"], "handler": "native_sdk"}
        advance("authorizing", detail="验证本机 v5 授权")
        with NativeV5(config["sdk"], qsv.ticket()) as sdk:
            advance("extracting", detail="提取视频与独立音轨")
            require_completed(config, source)
            qsv.extract(work / "outer.ts", work / "audio.m4a", config["copy_buffer_bytes"],
                        lambda done, total: advance("extracting", progress=done * 100 / total))
            advance("decrypting", detail="按分段标记处理加密或明文视频，保留原始时间戳")
            report["conversion"] = TransportDecoder(sdk, config["max_pes_bytes"]).convert(
                work / "outer.ts", work / "clear.ts",
                lambda done, total: advance("decrypting", progress=done * 100 / total), config["copy_buffer_bytes"])
        report["format"].update(protection=report["conversion"]["protection"],
                                stream_type=report["conversion"]["stream_type"],
                                descriptor_styles=report["conversion"]["descriptor_styles"])
        report["status"] = "extracted"
        return report
    except BaseException as error:
        active_error = error
        report.update(status=error_status(error), error=str(error))
        raise
    finally:
        report["preparation_seconds"] = round(time.monotonic() - started, 2)
        report["elapsed_seconds"] = report["preparation_seconds"]
        save_report(report, work, active_error)


def mux(config: dict, report: dict, work: Path, notify: Callable,
        cancel_event: Event | None = None) -> dict:
    """按 config 封装 report 对应的媒体；work 为工作目录，notify 接收进度，cancel_event 可取消；返回待验证报告。"""
    started, active_error = time.monotonic(), None
    candidate = Path(report["candidate"])
    try:
        check_cancel(cancel_event)
        notify("muxing", detail="无损封装 MP4")
        run_process([str(config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning", "-n",
                     "-i", str(work / "clear.ts"), "-i", str(work / "audio.m4a"), "-map", "0:v:0", "-map", "1:a:0",
                     "-map", "0:a:0?", "-c", "copy",
                     "-tag:v", "hvc1" if report["conversion"]["stream_type"] == 0x24 else "avc1",
                     "-disposition:a:0", "default", "-disposition:a:1", "0", "-movflags", "+faststart", str(candidate)],
                    work / "mux.log", config, cancel_event=cancel_event)
        check_cancel(cancel_event)
        report["status"] = "prepared"
        return report
    except BaseException as error:
        active_error = error
        report.update(status=error_status(error), error=str(error))
        raise
    finally:
        report["preparation_seconds"] = round(report["preparation_seconds"] + time.monotonic() - started, 2)
        report["elapsed_seconds"] = report["preparation_seconds"]
        save_report(report, work, active_error)


def verify(config: dict, report: dict, work: Path, notify: Callable, device: str,
           cancel_event: Event | None = None) -> dict:
    """按 config 用 device（cpu/gpu）解码 report 的候选，日志保存在 work；
    notify 接收设备和进度，cancel_event 可取消；返回解码证明，失败保留现场。
    """
    started, active_error = time.monotonic(), None
    candidate = Path(report["candidate"])
    try:
        check_cancel(cancel_event)
        report["verification"] = {"device": device}
        notify("verifying", device=device, detail=f"完整解码验证全部视频帧及音轨（{device.upper()}）")
        frames = report["conversion"]["video_pes"]
        progress_path = work / "decode_progress.log"
        run_process(decode_command(config, candidate, progress_path, device), work / "decode.log", config,
                    lambda values: notify("verifying", device=device,
                                          progress=min(99.9, int(values.get("frame", 0)) * 100 / frames)),
                    progress_path, cancel_event=cancel_event)
        report["verification"] = {**validate_progress(read_progress(progress_path), frames), "device": device}
        report["status"] = "decoded"
        return report
    except BaseException as error:
        active_error = error
        report.update(status=error_status(error), error=str(error))
        raise
    finally:
        report["verification_seconds"] = round(time.monotonic() - started, 2)
        report["elapsed_seconds"] = round(report["preparation_seconds"] + report["verification_seconds"], 2)
        save_report(report, work, active_error)


def finalize_verification(config: dict, report: dict, work: Path, notify: Callable,
                          cancel_event: Event | None = None) -> dict:
    """完成 report 的来源及成品校验；config 控制读取，work 保存日志，notify 接收状态，cancel_event 可取消；返回完整证明。"""
    started, active_error = time.monotonic(), None
    source, candidate = Path(report["source"]), Path(report["candidate"])
    try:
        check_cancel(cancel_event)
        notify("checking", device=report["verification"]["device"], detail="校验源文件、成品指纹和媒体信息")
        require_completed(config, source)
        original = report["source_fingerprint"]
        current = fingerprint(source, config["copy_buffer_bytes"], force=True, cancel_event=cancel_event)
        report["source_unchanged"] = current["sha256"] == original["sha256"]
        if not report["source_unchanged"]:
            raise RuntimeError("转换期间源文件发生变化，停止发布")
        report["output_fingerprint"] = fingerprint(candidate, config["copy_buffer_bytes"], force=True,
                                                   cancel_event=cancel_event)
        report["media"] = probe_media(config, candidate, work / "media.log", cancel_event=cancel_event)
        report["status"] = "verified"
        return report
    except BaseException as error:
        active_error = error
        report.update(status=error_status(error), error=str(error))
        raise
    finally:
        report["verification_seconds"] = round(report["verification_seconds"] + time.monotonic() - started, 2)
        report["elapsed_seconds"] = round(report["preparation_seconds"] + report["verification_seconds"], 2)
        save_report(report, work, active_error)


def convert(config: dict, source: Path, work: Path, notify: Callable) -> dict:
    """同步准备并验证 source；config 决定设备（均开启时优先 GPU），work/notify 为工作目录及进度接口；
    返回完整验证报告。此接口用于单文件检查，正式批次通过独立阶段调度实现流水线。
    """
    report = prepare(config, source, work, notify)
    report = mux(config, report, work, notify)
    device = "gpu" if config["decode_verification"]["gpu"] else "cpu"
    report = verify(config, report, work, notify, device)
    return finalize_verification(config, report, work, notify)


def cleanup(work: Path) -> None:
    """在成品和索引已提交后清理 work 的中间媒体；只删除四个约定文件，日志及报告保留，无返回值。"""
    for name in ("outer.ts", "audio.m4a", "clear.ts", "validated.partial.mp4"):
        (work / name).unlink(missing_ok=True)
