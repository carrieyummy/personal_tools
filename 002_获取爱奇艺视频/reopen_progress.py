"""从已保存的爱奇艺下载日志启动只读进度页面。"""

import argparse
from pathlib import Path
import sys

from qsv_local.progress_view import serve


def main() -> int:
    """读取可选主配置路径并运行进度页面；正常结束返回 0，失败返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "configs" / "config.yaml",
                        help="主 YAML 配置，默认使用工具目录下的 configs/config.yaml")
    args = parser.parse_args()
    try:
        serve(args.config)
    except KeyboardInterrupt:
        print("只读进度页面已关闭。")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"无法打开进度页面：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
