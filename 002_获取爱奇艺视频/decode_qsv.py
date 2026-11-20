"""批量转换本机授权有效的 v5 QSV，复用成功结果并保留内容变化后的新版本。"""

import argparse
import json
from pathlib import Path
import sys

from qsv_local.configuration import read_config
from qsv_local.batch import run_batch


def main() -> int:
    """解析 --config YAML 路径并执行批次；全部成功/复用返回 0，失败返回 1，参数错误返回 2，中断返回 130。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,  default=Path(__file__).resolve().parent / "configs" / "config.yaml", help="YAML 配置路径（相对当前工作目录），文件内路径按配置注释解析")
    args = parser.parse_args()
    try:
        report = run_batch(read_config(args.config))
    except KeyboardInterrupt:
        print("已中止，成功成品及转换记录已保留。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"转换失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
