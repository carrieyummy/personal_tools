"""只读客户端近期日志中的播放协议参数；不输出原始日志、账号票据或可复用的签名地址。"""

import argparse
from collections import Counter
from html import unescape
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.configuration import resolve_path
from local.storage import atomic_json


def main() -> None:
    """按必填 --config 限定日志范围，提取白名单协议参数并去重；保存脱敏研究报告，无返回值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    path = args.config.resolve()
    research = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    settings = research["native_logs"]
    root = resolve_path(settings["root"], path.parent)
    files = sorted({file for pattern in settings["patterns"] for file in root.glob(pattern)},
                   key=lambda file: file.stat().st_mtime, reverse=True)[:settings["max_files"]]
    reports = []
    for file in files:
        with file.open("rb") as stream:
            stream.seek(max(0, file.stat().st_size - settings["max_bytes"]))
            text = stream.read(settings["max_bytes"]).decode("utf-8", errors="replace")
        text = unescape(text.replace("\\/", "/"))
        summaries, count = {}, Counter()
        for match in re.finditer(r'https?://[^\s<>"\x00]+', text):
            parsed = urlsplit(match[0])
            endpoint = parsed.path.rsplit("/", 1)[-1]
            if endpoint not in settings["endpoints"]:
                continue
            query = parse_qs(parsed.query)
            selected = {name: query[name] for name in settings["query_fields"] if name in query}
            key = json.dumps({"host": parsed.hostname, "endpoint": endpoint, "parameters": selected}, sort_keys=True)
            summaries[key] = {"host": parsed.hostname, "endpoint": endpoint, "parameters": selected,
                              "query_names": sorted(query)}
            count[key] += 1
        report = {"file": file.name, "bytes_scanned": len(text.encode("utf-8")),
                  "requests": [{**value, "count": count[key]} for key, value in summaries.items()]}
        reports.append(report)
        print(json.dumps({"file": file.name, "unique_requests": len(summaries),
                          "requests": report["requests"]}, ensure_ascii=False), flush=True)
    target = resolve_path(research["report_dir"], path.parent) / "native_requests.json"
    atomic_json(target, {"files": reports})
    print(f"客户端协议参数报告：{target}")


if __name__ == "__main__":
    main()
