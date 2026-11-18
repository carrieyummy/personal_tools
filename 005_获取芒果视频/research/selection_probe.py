"""对比配置中的源选择参数；只输出结构和调度可用性，绝不输出地址或认证数据。"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.client import NativeClient, normalize_url
from local.configuration import read_config, read_yaml


def inspect(data: dict) -> dict:
    """提取源响应结构；data 为接口 data；返回脱敏字段和链接形状。"""
    def node(value: dict) -> dict:
        """描述单源 value；返回身份、字段和调度存在标记，不返回网络地址。"""
        result = {key: value.get(key) for key in ("fileId", "definition", "originDef", "videoFormat", "fileFormat")}
        result["fields"] = list(value)
        result["dispatch"] = bool(value.get("disp", {}).get("info"))
        for key in ("url", "url2"):
            if isinstance(value.get(key), str):
                parsed = urlsplit(value[key])
                result[key] = {"host": parsed.hostname, "path": parsed.path, "query_keys": list(parse_qs(parsed.query))}
        result["backups"] = [node(child) for child in value.get("bakStreams", [])]
        return result
    return {"quality": data.get("streamQuality"), "sources": [node(value) for value in data["videoSources"]]}


def main() -> int:
    """按命令行配置进行选择对比；无参数；写脱敏结果，返回 0。"""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "list", "variants", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    config = read_config(args.config)
    reports = []
    for item in read_yaml(args.list)["download_list"]:
        _, clip_id, video_id = normalize_url(item)
        initial = NativeClient(config).source(clip_id, video_id)
        definition = str(initial["videoSources"][0]["definition"])
        report = {"clip_id": clip_id, "video_id": video_id, "variants": []}
        for variant in read_yaml(args.variants)["variants"]:
            temporary = deepcopy(config)
            temporary["profile"]["playback"]["query"].update(variant["query"])
            client = NativeClient(temporary)
            data = client.source(clip_id, video_id, definition)
            result = {"name": variant["name"], **inspect(data)}
            report["variants"].append(result)
            dispatched = [(source["fileId"], source["videoFormat"]) for source in result["sources"] if source["dispatch"]]
            print(clip_id, variant["name"], dispatched, flush=True)
        reports.append(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
