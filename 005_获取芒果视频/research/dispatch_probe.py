"""直接检查授权源的调度接口；签名地址和票据只驻留内存，报告仅包含响应状态结构。"""

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import parse_qsl, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local.client import NativeClient, normalize_url, safe_message
from local.configuration import read_config, read_yaml


def main() -> int:
    """根据 CLI 指定配置与清单检查调度；无参数；保存脱敏报告，返回 0。"""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "list", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    client = NativeClient(read_config(args.config))
    reports = []
    for item in read_yaml(args.list)["download_list"]:
        _, clip_id, video_id = normalize_url(item["url"])
        initial = client.source(clip_id, video_id)
        data = client.source(clip_id, video_id, str(initial["videoSources"][0]["definition"]))
        selected = data["videoSources"][0]
        report = {"clip_id": clip_id, "fileId": selected["fileId"], "dispatch": []}
        parsed = urlsplit(selected["url"])
        for scheme in ("https", "http"):
            endpoint = urlunsplit((scheme, parsed.netloc, parsed.path, "", ""))
            try:
                value, summary = client.transport.json(endpoint, dict(parse_qsl(parsed.query)))
                report["dispatch"].append({"scheme": scheme, **summary, "fields": list(value), "status": value.get("status"),
                                           "error": safe_message(value.get("err_msg", "")), "has_info": isinstance(value.get("info"), str) and value["info"].startswith(("http://", "https://"))})
            except Exception as error:
                report["dispatch"].append({"scheme": scheme, "error": safe_message(error)})
        print(json.dumps(report, ensure_ascii=False), flush=True)
        reports.append(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
