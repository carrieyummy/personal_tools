"""优酷 SVIP 清晰度研究入口；统一定义命令行参数并执行只读采样或离线分析。"""

from __future__ import annotations

import argparse
import json
import sys
import sqlite3
from pathlib import Path

from lib.probe import analyze_ups, load_config, scan_cache, snapshot, source_evidence, write_report
from lib.native import native_signature, signature_summary
from lib.online import online_probe
from lib.playback_log import scan_playback_log
from lib.live import live_probe
from lib.media import sample_media, verify_media
from lib.restoration import restore_samples


def main(argv: list[str] | None = None) -> int:
    """运行研究命令；argv 为命令行参数或 None；成功返回 0，输入或采样失败返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="研究 YAML；相对路径以当前目录为基准")
    parser.add_argument("--action", required=True, choices=("snapshot", "source", "cache", "ups", "native", "online", "playback", "live", "media", "media-verify", "media-restore"),
                        help="环境、源码、缓存、UPS、签名、会话、日志、实时上下文；media 抽样媒体，media-verify 本地解码抽样")
    parser.add_argument("--input", type=Path, help="ups 必填原始 JSON；media-verify/media-restore 必填媒体报告；其他操作不适用")
    parser.add_argument("--pid", type=int, help="live/media/media-restore 必填：已打开配置样本的优酷主进程 PID")
    args = parser.parse_args(argv)
    if args.action in ("ups", "media-verify", "media-restore") and args.input is None:
        parser.error("ups/media-verify/media-restore 必须提供 --input")
    if args.action not in ("ups", "media-verify", "media-restore") and args.input is not None:
        parser.error("--input 仅适用于 ups/media-verify/media-restore")
    if args.action in ("live", "media", "media-restore") and (args.pid is None or args.pid <= 0):
        parser.error("live/media/media-restore 必须提供正整数 --pid")
    if args.action not in ("live", "media", "media-restore") and args.pid is not None:
        parser.error("--pid 仅适用于 live/media/media-restore")
    try:
        config = load_config(args.config)
        if args.action == "snapshot":
            result = snapshot(config)
        elif args.action == "source":
            result = source_evidence(config)
        elif args.action == "cache":
            result = scan_cache(config)
        elif args.action == "native":
            result = signature_summary(native_signature(config))
        elif args.action == "online":
            result = online_probe(config)
        elif args.action == "playback":
            result = scan_playback_log(config)
        elif args.action == "live":
            result = live_probe(config, args.pid)
        elif args.action == "media":
            result = sample_media(config, args.pid)
        elif args.action == "media-verify":
            result = verify_media(config, args.input)
        elif args.action == "media-restore":
            result = restore_samples(config, args.pid, args.input)
        else:
            result = analyze_ups(config, args.input)
        target = write_report(config, args.action + ".json", result)
        print(json.dumps({"report": str(target), "action": args.action}, ensure_ascii=False))
        if args.action == "media-verify" and not result["all_samples_decoded"]:
            return 1
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(f"研究操作失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
