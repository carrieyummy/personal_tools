"""定义 B 站工具命令行参数、校验和退出码；业务逻辑由 bilibili_downloader 包执行。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bilibili_downloader import application
from bilibili_downloader.manifest import is_manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 argv（None 使用进程参数）；返回模式、链接或清单输入、主配置及按模式设置默认值的拼接选项。"""
    parser = argparse.ArgumentParser(description="B 站投稿分P、番剧单集/整季/关联所有季和 YAML 清单下载；关联季共用一次逐集档位排名选择，其他来源选择画质上限，音视频仅无损封装")
    parser.add_argument("--url", action="append", help="视频/番剧链接、BV/AV 或 ep/ss 号、本地 YAML 下载清单；可重复指定并混用，相对清单路径以当前工作目录为准")
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "config" / "config.yaml", help="YAML 配置路径（默认脚本目录下的 config/config.yaml）")
    parser.add_argument("--cookies", type=Path, help="默认 Chrome 导出的 Cookie JSON 文件；覆盖配置中的认证方式和文件路径")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--login-only", action="store_true", help="仅验证配置的登录态；默认读取 Chrome 导出的 Cookie")
    mode.add_argument("--list-formats", action="store_true", help="仅查看当前账号可下载画质及字幕，不确认或下载")
    mode.add_argument("--merge-video", metavar="BV或ss号", help="离线拼接已下载的视频分 P 或番剧正片；接受 BV/ss 号及对应链接，不需要登录")
    parser.add_argument("--merge-parts", metavar="选项或分组表达式", help="用于 --merge-video、--all-parts 下载或 YAML 清单；-1 不拼接、0 拼接全部，其他值如 1-5,8;10-12；下载默认 -1，离线默认 0；清单项/全局配置优先")
    mode.add_argument("--refresh-metadata", action="store_true", help="批量迁移并补生成已有下载的 video_info.json，不下载或拼接视频")
    parser.add_argument("--all-parts", action="store_true", help="投稿下载全部分P，番剧下载同季正片；未启用时按 p/ep 下载、ss 取首集；清单 all-parts 优先；配置 download.all_seasons 开启时番剧逐季下载全部正片")
    args = parser.parse_args(argv)
    if args.refresh_metadata and (args.url or args.all_parts):
        parser.error("--refresh-metadata 不能与 --url 或 --all-parts 同用")
    if args.merge_parts is not None and not (args.merge_video or ((args.all_parts or any(is_manifest(value) for value in (args.url or []))) and not (args.login_only or args.list_formats or args.refresh_metadata))):
        parser.error("--merge-parts 只能与 --merge-video、--all-parts 下载或 YAML 下载清单同用")
    if args.merge_parts is None:
        args.merge_parts = "0" if args.merge_video else "-1"
    return args


def main(argv: list[str] | None = None) -> int:
    """以 argv 参数列表（None 使用进程参数）运行入口；返回退出码，并向终端报告可操作的错误。"""
    try:
        return application.run(parse_args(argv))
    except ModuleNotFoundError as exc:
        print(f"缺少依赖 {exc.name}；请在工作区先运行“准备环境”。", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n已中止；下载流和断点文件已保留。", file=sys.stderr)
        return 130
    except EOFError:
        print("没有收到视频链接；请通过工作区输入或 --url 指定链接。", file=sys.stderr)
    except Exception as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
