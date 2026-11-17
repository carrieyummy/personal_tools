"""保持当前播放窗口不变，验证指定声明画质排名能否由原生 SDK 精确选择。"""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.playback.configuration import read_profile
from qsv_local.playback.metadata import linked_videos
from qsv_local.playback.source import select_source


def main() -> int:
    """解析 --config/--index/--rank 并精确验收原生选档；成功返回 0，失败返回 1，不获取整集媒体。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="实际播放专项配置")
    parser.add_argument("--index", type=int, required=True, help="当前下载清单下标")
    parser.add_argument("--rank", type=int, required=True, help="本次验证的正整数画质排名")
    args = parser.parse_args()
    if args.index < 0 or args.rank <= 0:
        parser.error("index 必须为非负整数，rank 必须为正整数")
    try:
        profile = read_profile(args.config)
        video = linked_videos(profile, [args.index])[0]
        result = select_source(profile, video, args.rank)
        print(f"精确选档通过：{video['title']} 第 {video['episode']} 集，rank={args.rank}，{result.selected['scrsz']}，HDR={result.selected['dr']}")
        return 0
    except Exception as error:
        print(f"选档验收失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
