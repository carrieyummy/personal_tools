"""按专项配置实网验收：分享链接、已有登录态、芒臻完整清单与分片解码，全程不操作界面。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research.playback_probe import load_config, resolve_playback, save_report


def main() -> int:
    """执行配置中全部样本的实网验收；无参数；返回 0 表示通过，1 表示未通过。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="研究专项 YAML，提供样本、登录态位置及验收策略。")
    args = parser.parse_args()
    config = load_config(args.config)
    report = resolve_playback(config)
    report["validation_scope"] = "完整清单及配置位置的完整分片解码；不等于全片下载或全片解码。"
    path = save_report(report, config, "playback_verification.json")
    print(path.relative_to(PROJECT_ROOT))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
