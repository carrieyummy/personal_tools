"""只读比较同一所选画质各 CDN 的响应头；不下载分片、不改变正式画质或代理配置。"""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.configuration import load_config, resolve_path
from local.playback.client import NativeClient, formats, video_info
from local.playback.tasks import requests
from local.playback.hls import parse_media
from local.playback.transport import safe_message
from local.storage import atomic_json


def main() -> None:
    """按 --config 的研究目标读取最高档的同源 CDN 响应头，保存不含完整地址的脱敏比较报告。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    path = args.config.resolve()
    research = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    config = load_config(resolve_path(research["config"], path.parent), source="url")
    client = NativeClient(config)
    report = []
    for item in requests(config):
        if research["title"] and item.note != research["title"]:
            continue
        initial = client.source(item.series_id, item.video_id)
        choice = formats(initial, client.profile["playback"])[item.quality - 1]
        video = video_info(client.source(item.series_id, item.video_id, choice["name"]))
        result = {"note": item.note, "format_id": choice["id"], "cdns": []}
        for mirror in video["ul"]["ui"]:
            url = mirror["url"]
            started = time.monotonic()
            entry = {"host": urlsplit(url).hostname}
            try:
                with client.transport.open(url, method="HEAD") as response:
                    entry.update(status=response.code, headers={key: response.headers.get(key) for key in
                                 ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")})
            except Exception as error:
                entry["error"] = safe_message(error)
            entry["seconds"] = round(time.monotonic() - started, 3)
            result["cdns"].append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
        report.append(result)
    directory = resolve_path(research["report_dir"], path.parent)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "delivery_headers.json", {"items": report})


if __name__ == "__main__":
    main()
