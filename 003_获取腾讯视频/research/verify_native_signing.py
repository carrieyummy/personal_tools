"""观察候选播放器的真实签名 ABI，按当前链接清单验收后原子更新签名专项配置。"""

import argparse
import json
from pathlib import Path
import sys
from threading import Event
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.configuration import resolve_path
from local.playback.signing_compat import validate_candidate
from local.playback.tasks import requests
from local.playback.transport import safe_message
from local.storage import atomic_json
from research.settings import load_research


def main() -> int:
    """解析 --config/可选 --pid，验收全部链接并更新签名配置；保存脱敏报告，成功返回 0，否则返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="研究 YAML，引用主配置及当前链接清单")
    parser.add_argument("--pid", type=int, help="可选：指定腾讯播放器进程；省略时必须唯一匹配")
    args = parser.parse_args()
    if args.pid is not None and args.pid <= 0:
        parser.error("--pid 必须为正整数")
    research, config = load_research(args.config.resolve())
    observation = research["native_observation"]["watch_seconds"]
    transfer = research["transfer_probe"]
    width = research["native_observation"]["signing"]["paired_frame_width"]
    maximum = research["native_observation"]["max_events"]
    if type(maximum) is not int or maximum <= 0:
        parser.error("native_observation.max_events 必须为正整数")
    if type(width) is not int or width <= 0:
        parser.error("native_observation.signing.paired_frame_width 必须为正整数")
    if type(observation) not in (int, float) or not 0 < observation < float("inf"):
        parser.error("native_observation.watch_seconds 必须是有限正数")
    if type(transfer["sample_bytes"]) is not int or transfer["sample_bytes"] <= 0 or type(transfer["timeout_seconds"]) not in (int, float) or not 0 < transfer["timeout_seconds"] < float("inf"):
        parser.error("transfer_probe 的字节及超时上限必须为正数且有限")
    profile = resolve_path(config["playback"]["playback"]["ckey_profile"], config["playback_profile"].parent)
    report = {"status": "failed", "profile_updated": False}
    try:
        requests(config, require_single_video=False)  # 研究整剧链接允许稍后按目录选取样本。
        report = validate_candidate(config, research, profile, args.pid, Event())
    except KeyboardInterrupt:
        report["error"] = "用户取消候选签名验收"
    except Exception as error:
        report["error"] = safe_message(error)
    target = resolve_path(research["report_dir"], args.config.resolve().parent) / f"verify_native_signing_{uuid4().hex}.json"
    atomic_json(target, report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    print(f"候选签名验收报告：{target}", flush=True)
    return int(report["status"] != "verified")


if __name__ == "__main__":
    raise SystemExit(main())
