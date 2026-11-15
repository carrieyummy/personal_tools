"""比较同一已验证 MP4 的 CPU/GPU 完整解码及多进程吞吐，保留可复核的命令与结果。"""

import argparse
from collections import deque
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.configuration import read_config, resolve_path
from qsv_local.process import read_progress
from qsv_local.storage import atomic_json, publish
from qsv_local.verification import decode_command, validate_progress


def read_benchmark(path: Path) -> dict:
    """读取专项配置 path 并校验任务数、预热及后端；返回含绝对报告路径的配置字典。"""
    path = path.expanduser().resolve()
    settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    for name in ("jobs_per_case", "repetitions", "warmup_seconds", "gpu_sample_interval_ms"):
        if type(settings[name]) is not int or settings[name] <= 0:
            raise ValueError(f"benchmark.{name} 必须为正整数")
    subdir = settings["work_subdir"]
    if not isinstance(subdir, str) or not subdir or Path(subdir).name != subdir or subdir in (".", ".."):
        raise ValueError("benchmark.work_subdir 必须为单个目录名")
    settings["results_dir"] = resolve_path(settings["results_dir"], path.parent)
    names = set()
    if not settings["cases"]:
        raise ValueError("benchmark.cases 不能为空")
    for case in settings["cases"]:
        name = case["name"]
        if (not isinstance(name, str) or not name or not name.isascii()
                or not all(char.isalnum() or char in "_-" for char in name) or name in names):
            raise ValueError("benchmark.case.name 必须为唯一的 ASCII 字母、数字、下划线或连字符名称")
        names.add(name)
        backends = case["backend"] if isinstance(case["backend"], list) else [case["backend"]]
        if not backends or any(backend not in ("cpu", "cuda") for backend in backends):
            raise ValueError("benchmark.case.backend 必须为 cpu、cuda 或由两者组成的非空列表")
        if type(case["workers"]) is not int or not 1 <= case["workers"] <= settings["jobs_per_case"]:
            raise ValueError("benchmark.case.workers 必须为 1 到 jobs_per_case 之间的整数")
    return settings


def select_source(root: Path) -> tuple[Path, dict]:
    """在输出根目录 root 中选最新登记成功且有完整验证记录的 MP4；返回文件和索引条目，不修改索引。"""
    candidates = []
    for index_path in root.rglob("video_info.json"):
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for entry in index.get("files", []):
            candidate = (index_path.parent / entry["path"]).resolve()
            if (entry.get("status") == "completed" and entry.get("verification", {}).get("frames", 0) > 0
                    and candidate.is_relative_to(root) and candidate.is_file()):
                candidates.append((entry["saved_at"], str(candidate), candidate, entry))
    if not candidates:
        raise ValueError("output_path 中没有登记成功且保留完整帧数验证记录的 MP4")
    _, _, source, entry = max(candidates, key=lambda item: item[:2])
    return source, entry


def stop_process(process: subprocess.Popen, config: dict) -> None:
    """回收本脚本启动的 process；config 提供终止等待时间，无返回值；超时后强制回收。"""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=config["runtime"]["process_terminate_timeout_seconds"])
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_case(config: dict, settings: dict, source: Path, folder: Path, case: dict,
             expected_frames: int, seconds: int | None = None) -> dict:
    """执行 case 的完整任务或 seconds 秒预热；config/settings 为运行配置，source 为本地输入，
    folder 独占保存日志，expected_frames 为正式任务的预期帧数；backend 列表按任务序号循环分配；
    返回耗时和验证结果，中断回收全部子进程。
    """
    folder.mkdir(parents=True, exist_ok=False)
    count = settings["jobs_per_case"] if seconds is None else 1
    backends = case["backend"] if isinstance(case["backend"], list) else [case["backend"]]
    pending, active, results = deque(range(count)), [], []
    monitor, monitor_log = None, None
    started = time.monotonic()
    result = {"name": case["name"], "backend": case["backend"], "workers": case["workers"],
              "jobs": results, "status": "running", "warmup_seconds": seconds}
    try:
        gpu_tool = shutil.which("nvidia-smi")
        if gpu_tool:
            monitor_log = (folder / "gpu.csv").open("wb")
            monitor = subprocess.Popen(
                [gpu_tool, "--query-gpu=timestamp,utilization.gpu,utilization.decoder,memory.used",
                 "--format=csv,noheader,nounits", "-lms", str(settings["gpu_sample_interval_ms"])],
                stdin=subprocess.DEVNULL, stdout=monitor_log, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        started = time.monotonic()
        while pending or active:
            while pending and len(active) < case["workers"]:
                number = pending.popleft()
                progress = folder / f"job_{number}.progress.log"
                backend = backends[number % len(backends)]
                command = decode_command(config, source, progress, "gpu" if backend == "cuda" else "cpu", seconds)
                log = (folder / f"job_{number}.stderr.log").open("xb")
                try:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except BaseException:
                    log.close()
                    raise
                active.append({"number": number, "process": process, "log": log, "progress": progress,
                               "backend": backend, "command": command, "started": time.monotonic()})
            for job in active[:]:
                process = job["process"]
                elapsed = time.monotonic() - job["started"]
                if process.poll() is None:
                    if elapsed > config["process_timeout_seconds"]:
                        raise TimeoutError(f"{case['name']} job {job['number']} 超时")
                    continue
                progress = read_progress(job["progress"])
                frames = int(progress.get("frame", 0))
                passed = process.returncode == 0
                if passed:
                    try:
                        validate_progress(progress, expected_frames if seconds is None else None)
                    except (RuntimeError, ValueError, KeyError):
                        passed = False
                results.append({"number": job["number"], "backend": job["backend"], "elapsed_seconds": round(elapsed, 4),
                                "returncode": process.returncode, "passed": passed, "frames": frames,
                                "progress": progress, "command": job["command"]})
                job["log"].close()
                active.remove(job)
                atomic_json(folder / "result.json", result)
                if not passed:
                    raise RuntimeError(f"{case['name']} job {job['number']} 解码或帧数验证失败，见 {folder}")
            time.sleep(config["runtime"]["cancel_poll_interval_seconds"])
        result["status"] = "passed"
        return result
    except BaseException as error:
        result.update(status="failed", error=str(error) or type(error).__name__)
        raise
    finally:
        result["wall_seconds"] = round(time.monotonic() - started, 4)
        result["videos_per_minute"] = round(len(results) * 60 / result["wall_seconds"], 4)
        for job in active:
            stop_process(job["process"], config)
            job["log"].close()
        if monitor is not None:
            stop_process(monitor, config)
        if monitor_log is not None:
            monitor_log.close()
        atomic_json(folder / "result.json", result)


def main() -> int:
    """解析主配置、专项配置及可选分组筛选，比较完整视频解码；全部完成返回 0，失败返回 1，中断返回 130。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="主 YAML 配置，复用输出、工作目录和 FFmpeg 参数")
    parser.add_argument("--benchmark-config", type=Path, required=True, help="性能比较专项 YAML 配置")
    parser.add_argument("--case", action="append", help="可选：只运行指定的配置组名；可重复传入，省略时运行全部组")
    args = parser.parse_args()
    config, settings = read_config(args.config), read_benchmark(args.benchmark_config)
    selected = set(args.case) if args.case else {case["name"] for case in settings["cases"]}
    unknown = selected - {case["name"] for case in settings["cases"]}
    if unknown:
        parser.error(f"未知比较组：{', '.join(sorted(unknown))}")
    run_id = datetime.now().strftime("%y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    folder = settings["results_dir"] / run_id
    folder.mkdir(parents=True, exist_ok=False)
    scratch = config["work_dir"] / settings["work_subdir"] / run_id
    local_source = scratch / "input.mp4"
    report = {"status": "preparing", "started": datetime.now().astimezone().isoformat(),
              "config": str(args.config.resolve()), "benchmark_config": str(args.benchmark_config.resolve()),
              "settings": {**settings, "results_dir": str(settings["results_dir"])},
              "selected_cases": sorted(selected),
              "scope": "完整视频及全部音轨解码；同一输入重复任务；不含 QSV 提取、SDK 处理及成品发布", "cases": []}
    try:
        source, entry = select_source(config["output_path"])
        report.update(source=str(source), media=entry["media"], verification=entry["verification"],
                      source_fingerprint=entry["output_fingerprint"])
        print(f"复制已验证成品到本地 SSD：{source.name}", flush=True)
        publish(source, local_source, entry["output_fingerprint"]["sha256"], config["copy_buffer_bytes"])
        report["status"] = "running"
        atomic_json(folder / "report.json", report)
        warmed = set()
        for repetition in range(settings["repetitions"]):
            for case in settings["cases"]:
                if case["name"] not in selected:
                    continue
                backends = case["backend"] if isinstance(case["backend"], list) else [case["backend"]]
                for backend in backends:
                    if backend not in warmed:
                        print(f"预热 {backend}：{settings['warmup_seconds']} 秒视频", flush=True)
                        run_case(config, settings, local_source, folder / f"warmup_{backend}",
                                 {**case, "backend": backend, "workers": 1},
                                 entry["verification"]["frames"], settings["warmup_seconds"])
                        warmed.add(backend)
                print(f"开始 {case['name']}：{settings['jobs_per_case']} 次完整解码，并发 {case['workers']}", flush=True)
                result = run_case(config, settings, local_source, folder / f"round_{repetition}_{case['name']}",
                                  case, entry["verification"]["frames"])
                result["repetition"] = repetition
                report["cases"].append(result)
                atomic_json(folder / "report.json", report)
                print(f"完成 {case['name']}：{result['wall_seconds']:.2f} 秒，{result['videos_per_minute']:.3f} 次/分钟", flush=True)
        report["status"] = "passed"
        return 0
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        return 130
    except Exception as error:
        report.update(status="failed", error=str(error))
        print(str(error), file=sys.stderr, flush=True)
        return 1
    finally:
        report["finished"] = datetime.now().astimezone().isoformat()
        atomic_json(folder / "report.json", report)
        # 只删除本次独占目录中的输入副本；原始成品、目录和各组日志均保留。
        if local_source.is_relative_to(scratch) and scratch.is_relative_to(config["work_dir"]):
            local_source.unlink(missing_ok=True)
        print(f"比较报告：{folder / 'report.json'}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
