"""只读检查官方链接对应的播放缓存，不创建客户端离线下载任务，也不修改缓存。"""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.playback.cache import inspect_cache


def main() -> int:
    """读取 --config 并检查播放缓存；成功返回 0，失败返回 1，中断返回 130。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="播放缓存研究 YAML 路径，相对路径以该 YAML 为基准")
    args = parser.parse_args()
    try:
        report, output = inspect_cache(args.config)
        for link in report["links"]:
            streams = [row for row in report["coverage"] if row["tvid"] == link["tvid"]]
            print(f"[{link['index']}] {link['title']}，链接第 {link['episode']} 集，索引来源 {len(streams)} 个", flush=True)
        print(f"只读播放缓存报告：{output}", flush=True)
        return 0
    except KeyboardInterrupt:
        print("已停止只读播放缓存研究。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"播放缓存研究失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
