"""在隔离目录运行真实批次，记录吞吐、阶段耗时和整机资源采样。"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import platform
import subprocess
import tempfile
from threading import Event, Thread
import time

import psutil

from local.batch import run_batch
from local.configuration import load_config
from local.hls import discover_all
from local.native import latest_dll
from local.storage import atomic_json


def sample_resources(stop: Event, interval: float, samples: list) -> None:
    """每 interval 秒采样整机 CPU、内存、磁盘、网络和 NVIDIA GPU，直到 stop；写入 samples。"""
    psutil.cpu_percent(None)
    while not stop.is_set():
        sample = {"time": time.time(), "cpu_percent": psutil.cpu_percent(None),
                  "memory_percent": psutil.virtual_memory().percent}
        for name, reader in (("disk", psutil.disk_io_counters), ("network", psutil.net_io_counters)):
            try:
                counters = reader()
                sample[name] = counters._asdict() if counters else None
            except (OSError, RuntimeError, psutil.Error) as error:
                sample[name] = None
                sample[name + "_error"] = str(error)
        try:
            process = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,utilization.memory,utilization.decoder,memory.used",
                                      "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                     timeout=interval, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            sample["gpu"] = process.stdout.strip().splitlines() if process.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            sample["gpu"] = None
        samples.append(sample)
        stop.wait(interval)


def benchmark(config: dict, repeat: int, interval: float, report_path: Path) -> dict:
    """按 config 执行 repeat 次隔离批次；interval 为采样秒数，report_path 保存结果；返回汇总。"""
    tasks, skipped = discover_all(config)
    if not tasks:
        raise ValueError("当前配置没有可处理的已完成任务")
    result = {"selection": config["selection"], "sample_ids": [task["video_id"] for task in tasks],
              "sample_bytes": sum(task["media_bytes"] for task in tasks), "skipped": len(skipped),
              "max_in_flight": config["max_in_flight"], "decode_verification": config["decode_verification"],
              "system": {"platform": platform.platform(), "logical_cpus": psutil.cpu_count(),
                         "input_root": str(config["input_root"]), "output_root": str(config["output_path"]),
                         "work_root": str(config["work_dir"]), "ffmpeg": str(config["ffmpeg_path"]),
                         "client_dll": str(latest_dll(config["native"]["dll_root"], config["native"]["dll_name"])),
                         "client_dll_sha256": config["native"]["dll_sha256"]},
              "sampling_interval_seconds": interval,
              "measurement_notes": "CPU、内存、磁盘和网络为整机采样；磁盘和网络为累计计数；GPU 每行为利用率、显存利用率、解码器利用率和已用显存。资源持有时间可能嵌套，不能直接求和。",
              "runs": []}
    config["output_path"].mkdir(parents=True, exist_ok=True)
    config["work_dir"].mkdir(parents=True, exist_ok=True)
    for _ in range(repeat):
        with tempfile.TemporaryDirectory(prefix="benchmark_", dir=config["output_path"]) as output, \
             tempfile.TemporaryDirectory(prefix="benchmark_", dir=config["work_dir"]) as work:
            run_config = deepcopy(config)
            run_config["output_path"], run_config["work_dir"] = Path(output), Path(work)
            # 固定最初发现的已完成任务，避免下载状态在重复运行期间变化导致样本不一致。
            run_config["selection"] = {"video_ids": result["sample_ids"], "series_names": []}
            samples, stop = [], Event()
            sampler = Thread(target=sample_resources, args=(stop, interval, samples), daemon=True)
            sampler.start()
            started = time.monotonic()
            summary = None
            error = None
            try:
                summary = run_batch(run_config)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            finally:
                elapsed = time.monotonic() - started
                stop.set()
                sampler.join()
            state_path = run_config["output_path"] / run_config["logging"]["state_file"]
            history = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            records = history.get("runs", [])
            result["runs"].append({"elapsed_seconds": round(elapsed, 3), "summary": summary, "error": error,
                                   "journal": records[-1] if records else None, "system_samples": samples})
            atomic_json(report_path, result)
            if error:
                raise RuntimeError(error)
            if summary["failed"] or summary["interrupted"] or summary["completed"] != len(tasks):
                raise RuntimeError("基准批次未完整成功；详情见报告")
    return result


def main() -> int:
    """解析必填配置、并发、设备、次数、采样间隔及报告路径；成功返回 0。"""
    parser = argparse.ArgumentParser(description="使用配置选择的真实下载运行隔离性能基准")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--max-in-flight", type=int, required=True)
    parser.add_argument("--devices", choices=("cpu", "gpu", "both"), required=True)
    parser.add_argument("--video-id", action="append", help="可重复指定当前配置已选中的已完成视频 ID")
    parser.add_argument("--repeat", type=int, required=True)
    parser.add_argument("--sample-interval", type=float, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.max_in_flight <= 0 or args.repeat <= 0 or not 0 < args.sample_interval < float("inf"):
        parser.error("并发数、次数和有限采样间隔均须大于 0")
    report = args.report.expanduser().resolve()
    if report.exists():
        parser.error("报告路径已存在，拒绝覆盖")
    config = load_config(args.config, source="download")
    if args.video_id:
        available = {task["video_id"] for task in discover_all(config)[0]}
        if len(set(args.video_id)) != len(args.video_id) or not set(args.video_id).issubset(available):
            parser.error("视频 ID 重复或不在当前配置选择的已完成任务中")
        config["selection"] = {"video_ids": args.video_id, "series_names": []}
    config["max_in_flight"] = args.max_in_flight
    config["decode_verification"] = {"cpu": args.devices in ("cpu", "both"), "gpu": args.devices in ("gpu", "both")}
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("x", encoding="utf-8") as stream:
        json.dump({"status": "running"}, stream)
    benchmark(config, args.repeat, args.sample_interval, report)
    print(f"性能报告：{report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
