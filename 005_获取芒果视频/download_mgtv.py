"""芒果视频链接/YAML 批量下载入口；命令行定义集中于此，业务位于 local。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from local import application
from local.client import safe_message
from local.errors import Cancelled, ToolError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 argv（None 使用进程参数）；返回已校验模式、配置与链接参数，非法组合以退出码 2 结束。"""
    parser = argparse.ArgumentParser(description="读取芒果客户端登录态，按链接下载账号可获取的最高画质；逐集完整解码后发布 MP4")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "configs" / "config.yaml", help="主 YAML 配置；相对配置路径以当前工作目录为基准")
    parser.add_argument("--url", action="append", help="芒果视频链接或 YAML 清单路径，可重复；不传时交互输入")
    parser.add_argument("--all-parts", action=argparse.BooleanOptionalAction, default=None, help="下载同剧全部正片；--no-all-parts 仅链接集；覆盖清单及主配置")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--login-only", action="store_true", help="仅只读验证客户端登录态，不下载")
    mode.add_argument("--list-formats", action="store_true", help="只显示账号逐集画质及真实源元数据，不下载")
    args = parser.parse_args(argv)
    if args.login_only and (args.url or args.all_parts is not None):
        parser.error("--login-only 不能与 --url 或 --all-parts/--no-all-parts 同用")
    return args


def main(argv: list[str] | None = None) -> int:
    """运行 argv 所描述任务；None 使用进程参数；返回 0/1/2/130，并输出脱敏错误。"""
    args = parse_args(argv)
    try:
        return application.run(args)
    except (KeyboardInterrupt, Cancelled):
        print("已取消，工作现场已保留。", file=sys.stderr)
        return 130
    except EOFError:
        print("没有收到输入，请用 --url 指定链接或 YAML 清单。", file=sys.stderr)
        return 2
    except (ToolError, KeyError, TypeError, ValueError, OSError) as error:
        print(f"运行失败：{safe_message(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
