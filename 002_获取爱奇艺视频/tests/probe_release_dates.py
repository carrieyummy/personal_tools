"""只读核验当前清单的专辑上映日期与目录前缀，不请求播放 SDK、不启动下载、不写输出目录。"""

import argparse
from datetime import date
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.naming import output_name
from qsv_local.playback.configuration import read_profile
from qsv_local.playback.metadata import linked_videos
from qsv_local.playback.naming import dated_folder, validate_date_prefix


def main() -> int:
    """解析实际 --config，查询当前清单的公开专辑日期并输出新目录名；成功返回 0，失败返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="实际链接下载专项 YAML")
    args = parser.parse_args()
    try:
        profile = read_profile(args.config)
        naming = profile["_naming_snapshot"]
        validate_date_prefix(naming)
        seen = set()
        for video in linked_videos(profile):
            if video["album_id"] in seen:
                continue
            seen.add(video["album_id"])
            original, episode = output_name(naming, (video["album_id"], video["title"], str(video["episode"])), video["episode_title"])
            folder = dated_folder(naming, original, video["release_date"])
            print(json.dumps({"title": video["title"], "album_id": video["album_id"], "release_date": video["release_date"],
                              "album_publish_time": video["album_publish_time"], "folder": folder,
                              "episode_stem_unchanged": episode, "download_started": False}, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        print(f"上映日期只读核验失败：{type(error).__name__}：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
