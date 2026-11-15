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


def main(argv: list[str] | None = None) -> int:
    """运行研究命令；argv 为命令行参数或 None；成功返回 0，输入或采样失败返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="研究 YAML；相对路径以当前目录为基准")
    parser.add_argument("--action", required=True, choices=("snapshot", "source", "cache", "ups", "native", "online"),
                        help="snapshot 环境、source 源码、cache 缓存、ups 离线响应、native 签名、online 会话查询")
    parser.add_argument("--input", type=Path, help="仅 ups 操作必填：本地原始 JSON，不会复制到报告")
    args = parser.parse_args(argv)
    if args.action == "ups" and args.input is None:
        parser.error("--action ups 必须提供 --input")
    if args.action != "ups" and args.input is not None:
        parser.error("--input 仅适用于 --action ups")
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
        else:
            result = analyze_ups(config, args.input)
        target = write_report(config, args.action + ".json", result)
        print(json.dumps({"report": str(target), "action": args.action}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(f"研究操作失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
