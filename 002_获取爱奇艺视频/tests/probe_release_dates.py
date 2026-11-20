"""只读核验剧集或电影的上映日期和目录前缀，不请求播放 SDK、不启动下载、不写输出目录。"""

import argparse
from datetime import date
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.naming import output_name
from qsv_local.playback.configuration import read_profile
from qsv_local.playback.metadata import linked_videos
from qsv_local.playback.naming import dated_folder, movie_name, validate_date_prefix


def main() -> int:
    """读取实际 --config，查询清单节目上映日并输出新目录名；成功返回 0，失败返回 1。"""
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
            if video.get("is_movie"):
                print(json.dumps({"title": video["title"], "album_id": video["album_id"], "release_date": video["release_date"],
                                  "is_movie": True, "movie_template": naming["movie_template"],
                                  "filename_preview": movie_name(naming, video, "媒体信息待选档") + ".mp4",
                                  "download_started": False}, ensure_ascii=False), flush=True)
                continue
            original, episode = output_name(naming, (video["album_id"], video["title"], str(video["episode"])), video["episode_title"])
            folder = dated_folder(naming, original, video["release_date"])
            date_source = {"is_movie": True} if video.get("is_movie") else {"album_publish_time": video["album_publish_time"]}
            print(json.dumps({"title": video["title"], "album_id": video["album_id"], "release_date": video["release_date"],
                              **date_source, "folder": folder,
                              "episode_stem_unchanged": episode, "download_started": False}, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        print(f"上映日期只读核验失败：{type(error).__name__}：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
