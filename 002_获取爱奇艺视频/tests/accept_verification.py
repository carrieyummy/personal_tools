"""用已登记成品验收真实 CPU/GPU 验证及截断输入拒绝；不修改 QSV、正式成品或客户端数据库。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sys
import tempfile
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.configuration import read_config
from qsv_local.storage import atomic_json, publish, read_json
from qsv_local.verification import check_gpu
from qsv_local.workflow import verify
from tests.benchmark_decode import read_benchmark, select_source


def accept_one(config: dict, entry: dict, origin: dict, candidate: Path, work: Path, device: str,
               cancel_event: Event) -> dict:
    """用 config 验证 entry/origin 的 candidate，device 选 cpu/gpu，work 保存日志；cancel_event 可取消，返回证明。"""
    work.mkdir(parents=True)
    report = {"status": "prepared", "source": origin["source"], "candidate": str(candidate),
              "source_fingerprint": entry["source_fingerprint"], "conversion": entry["conversion"],
              "preparation_seconds": 0}

    def notify(stage: str, **fields) -> None:
        """接收验收 stage 及 fields；验收不启动正式批次日志服务，无返回值。"""
        return None

    result = verify(config, report, work, notify, device, cancel_event)
    return result["verification"]


def main() -> int:
    """解析主配置和性能专项配置，真实验收正常/截断成品；结果保存在专项报告根目录，全部通过返回 0。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="正式主配置，提供输出、工作目录、FFmpeg 和运行参数")
    parser.add_argument("--benchmark-config", type=Path, required=True, help="性能专项配置，只读取报告根目录")
    args = parser.parse_args()
    config, settings = read_config(args.config), read_benchmark(args.benchmark_config)
    cancel_event = Event()
    folder = settings["results_dir"] / ("acceptance_" + datetime.now().strftime("%y%m%d_%H%M%S"))
    folder.mkdir(parents=True, exist_ok=False)
    result = {"status": "running", "normal": {}, "truncated": {},
              "limitations": "截断输入被拒绝不证明 CPU/GPU 对所有损坏码流检测等价"}
    try:
        source, entry = select_source(config["output_path"])
        origin = read_json(Path(entry["work_dir"]) / "report.json")
        result.update(source=str(source), expected_frames=entry["verification"]["frames"])
        check_gpu(config, folder / "gpu_probe")
        config["work_dir"].mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="qsv-accept-", dir=config["work_dir"]) as temporary:
            candidate = Path(temporary) / "input.mp4"
            publish(source, candidate, entry["output_fingerprint"]["sha256"], config["copy_buffer_bytes"])
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {device: pool.submit(accept_one, config, deepcopy(entry), origin, candidate,
                                               folder / f"normal_{device}", device, cancel_event) for device in ("cpu", "gpu")}
                try:
                    for device, future in futures.items():
                        result["normal"][device] = future.result()
                        print(f"正常完整视频 {device} 验证通过：{result['normal'][device]['frames']} 帧", flush=True)
                except BaseException:
                    cancel_event.set()
                    raise
            # 只截断本次独占临时目录中的副本，保留原 MP4 的前半段，检验解码错误或缺帧能阻止通过。
            with candidate.open("r+b") as stream:
                stream.truncate(candidate.stat().st_size // 2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {device: pool.submit(accept_one, config, deepcopy(entry), origin, candidate,
                                               folder / f"truncated_{device}", device, cancel_event) for device in ("cpu", "gpu")}
                try:
                    for device, future in futures.items():
                        try:
                            future.result()
                        except RuntimeError as error:
                            result["truncated"][device] = {"rejected": True, "error": str(error)}
                            print(f"截断视频 {device} 被拒绝：{error}", flush=True)
                        else:
                            raise RuntimeError(f"截断视频被 {device} 错误接受")
                except BaseException:
                    cancel_event.set()
                    raise
        result["status"] = "passed"
        return 0
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        return 130
    except Exception as error:
        result.update(status="failed", error=str(error))
        print(str(error), file=sys.stderr, flush=True)
        return 1
    finally:
        atomic_json(folder / "acceptance.json", result)
        print(f"验收报告：{folder / 'acceptance.json'}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
