"""对同一批已验收成品比较固定后端分配的串行与并发完整解码；不重新下载或发布媒体。"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
from threading import BoundedSemaphore, Event
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.client import normalize_url
from local.configuration import read_config, read_yaml
from local.media import verify
from local.storage import file_hash, read_json


def main() -> int:
    """按 CLI 选择已验收样本和日志路径；无参数；写串并发完整验证报告，成功返回 0。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--list", type=Path, required=True)
    parser.add_argument("--items", type=int, nargs="+", required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    config, cancel = read_config(args.config), Event()
    devices = [device for device in ("gpu", "cpu") if config["decode_verification"][device]]
    slots = {device: BoundedSemaphore(1) for device in devices}
    index = read_json(config["output_path"] / config["logging"]["index_file"], {})["videos"]
    entries = read_yaml(args.list)["download_list"]
    samples = []
    for position, selected in enumerate(args.items):
        _, clip_id, video_id = normalize_url(entries[selected]["url"])
        record = index[f"{clip_id}/{video_id}"][-1]
        path = config["output_path"] / record["path"]
        if file_hash(path, config["download"]["buffer_bytes"], cancel) != record["sha256"]:
            raise ValueError("基准样本指纹不一致")
        samples.append((path, record, devices[position % len(devices)]))
    report = {"scope": "完整解码阶段；不包含网络下载、发布和初始指纹读取；每个样本串并发使用同一后端", "samples": [
        {"path": path.name, "sha256": record["sha256"], "device": device} for path, record, device in samples]}

    def execute(mode: str, sample):
        """执行一份 sample 的完整验证；mode 为日志组；返回解码证据，设备槽按配置启用情况限制。"""
        path, record, device = sample
        directory = args.report_dir.resolve() / mode / record["identity"]
        with slots[device]:
            return verify(config, path, device, record["hls"]["durations"], record["source"], cancel, directory)

    for mode in ("serial", "concurrent"):
        print(f"[{mode}] 完整解码开始", flush=True)
        started = time.monotonic()
        if mode == "serial":
            results = [execute(mode, sample) for sample in samples]
        else:
            with ThreadPoolExecutor(max_workers=len(devices)) as executor:
                results = list(executor.map(lambda sample: execute(mode, sample), samples))
        report[mode] = {"seconds": round(time.monotonic() - started, 3), "results": results}
        print(f"[{mode}] {report[mode]['seconds']} 秒", flush=True)
    report["speed_ratio"] = report["serial"]["seconds"] / report["concurrent"]["seconds"]
    for path, record, _ in samples:
        if file_hash(path, config["download"]["buffer_bytes"], cancel) != record["sha256"]:
            raise ValueError("基准期间源成品发生变化")
    args.report_dir.mkdir(parents=True, exist_ok=True)
    (args.report_dir / "benchmark_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
