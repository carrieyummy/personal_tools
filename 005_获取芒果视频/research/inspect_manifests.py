"""验证正式源选择及完整清单结构，仅保存脱敏报告，不保存签名地址。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.client import NativeClient, normalize_url
from local.configuration import read_config, read_yaml
from local.hls import fetch_text, playlists
from local.tasks import Episode, describe, resolved, streams


def main() -> int:
    """按 CLI 指定配置与清单检查最高源；无参数；写脱敏报告并返回失败计数是否非零。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--list", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    client = NativeClient(read_config(args.config))
    reports = []
    for item in read_yaml(args.list)["download_list"]:
        _, clip_id, video_id = normalize_url(item)
        report = {"clip_id": clip_id, "video_id": video_id}
        try:
            data = client.source(clip_id, video_id)
            report["formats"] = [describe(value) for value in streams(data)]
            probes = []
            for definition in dict.fromkeys([str(data["videoSources"][0]["definition"]), str(data["videoSources"][0].get("originDef", data["videoSources"][0]["definition"]))]):
                probe = client.source(clip_id, video_id, definition)
                probes.append({"definition": definition, "quality": probe.get("streamQuality"), "dispatches": [
                    {"fileId": value.get("fileId"), "definition": value.get("definition"), "codec": value.get("videoFormat"),
                     "has_dispatch": bool(value.get("disp", {}).get("info")), "backup": [{"fileId": backup.get("fileId"), "has_dispatch": bool(backup.get("disp", {}).get("info"))} for backup in value.get("bakStreams", [])]}
                    for value in probe["videoSources"]]})
            report["selection_probes"] = probes
            episode = Episode(clip_id, video_id, 1, "检查", "检查", True, 1)
            selected = resolved(client, episode)
            report["selected"] = describe(selected)
            text = fetch_text(client.transport, selected["disp"]["info"])
            report["tags"] = sorted({line.split(":")[0] for line in text.splitlines() if line.startswith("#")})
            media = playlists(client.transport, selected["disp"]["info"])
            report["tracks"] = [{"resources": len(value.resources), "duration": value.duration, "hash": value.identity,
                                  "initializations": sum(part.initial for part in value.resources)} for value in media]
            report["ok"] = True
        except Exception as error:
            from local.client import safe_message
            report.update(ok=False, error=safe_message(error))
        print(json.dumps(report, ensure_ascii=False), flush=True)
        reports.append(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(any(not report["ok"] for report in reports))


if __name__ == "__main__":
    raise SystemExit(main())
