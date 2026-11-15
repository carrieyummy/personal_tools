"""腾讯视频 URL 清单下载及本地缓存还原入口；模式与运行选项统一读取 YAML。"""

import argparse
from pathlib import Path
import sys

from local.configuration import load_config
from local.batch import run_batch
from local.playback.application import run_url_batch
from local.playback.transport import safe_message


def main() -> int:
    """解析必填 --config，按来源执行并报告汇总；返回 0 成功、1 失败、130 用户中断。"""
    parser = argparse.ArgumentParser(description="按 YAML 选择腾讯 URL 高清下载或已完成缓存还原，并完整验证 MP4")
    parser.add_argument("--config", type=Path, required=True, help="主 YAML 配置文件路径")
    arguments = parser.parse_args()
    try:
        config = load_config(arguments.config)
        runner = run_url_batch if config["source"] == "url" else run_batch
        summary = runner(config)
        print(f"批次汇总：{summary}", flush=True)
        return 1 if summary["failed"] else 0
    except KeyboardInterrupt:
        print("已中断，工作目录保留现场。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"处理失败：{safe_message(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
