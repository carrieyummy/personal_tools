"""优酷 URL 清单下载入口：配置驱动并发、原码流还原和 CPU/GPU 完整验证。"""

import argparse
from pathlib import Path
import sys

from local.application import run_url_batch
from local.configuration import load_config
from local.errors import safe_message


def main() -> int:
    """解析可选 --config 并执行完整批次；默认使用入口旁 configs/config.yaml，返回 0 成功、1 失败或跳过、130 用户取消。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,default=Path(__file__).resolve().parent / "configs/config.yaml",
                        help="主 YAML 配置文件路径；默认使用入口目录下的 configs/config.yaml")
    args = parser.parse_args()
    try:
        summary = run_url_batch(load_config(args.config))
        print(f"批次汇总：{summary}",flush=True)
        return 1 if summary["failed"] or summary["skipped"] or summary["interrupted"] else 0
    except KeyboardInterrupt:
        print("批次已取消，可靠断点保留。",file=sys.stderr)
        return 130
    except Exception as error:
        print("处理失败："+safe_message(error),file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
