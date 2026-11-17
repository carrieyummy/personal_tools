"""从 YAML 链接清单下载爱奇艺视频；先登录客户端并打开任意正片后暂停一次，保持客户端打开。"""

import argparse
from pathlib import Path
import sys

from qsv_local.playback.application import run_downloads


def main() -> int:
    """解析 --config、可选 --index/--episode/--plan；成功返回 0，单项或批次失败返回 1，中断返回 130。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="播放会话专项 YAML；输入清单、主配置及 SDK 适配均从该文件读取")
    parser.add_argument("--index", action="append", type=int, help="可选：只处理指定清单下标，从 0 开始，可重复指定；仍按该任务的 all-parts 展开")
    parser.add_argument("--episode", action="append", type=int, help="可选：过滤任务展开后的正整数集序，可重复指定；不覆盖 all-parts 限制")
    parser.add_argument("--plan", action="store_true", help="可选：只解析公开正片目录，不请求登录播放会话或获取媒体")
    args = parser.parse_args()
    if args.index is not None and (any(value < 0 for value in args.index) or len(set(args.index)) != len(args.index)):
        parser.error("--index 必须为不重复的非负整数")
    if args.episode is not None and (any(value <= 0 for value in args.episode) or len(set(args.episode)) != len(args.episode)):
        parser.error("--episode 必须为不重复的正整数")
    try:
        report, output = run_downloads(args.config, args.index, args.plan, args.episode)
        print(f"批次报告：{output}", flush=True)
        if not args.plan:
            print(f"完成 {report['completed']}，复用 {report['reused']}，失败 {report['failed']}，链接失败 {len(report['link_errors'])}", flush=True)
        return 1 if report["failed"] or report["link_errors"] else 0
    except KeyboardInterrupt:
        print("已停止链接下载，已完成分段及失败现场保留。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"链接下载失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
