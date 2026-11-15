"""显式实网单集验收：读取当前清单和原生登录态，核对真实画质/音质，可选全片下载与再次复用。"""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.configuration import load_config
from local.journal import Journal
from local.playback.application import UrlBatch
from local.playback.audio import AudioSelector
from local.playback.client import NativeClient, formats
from local.playback.source import resolve
from local.playback.tasks import requests, catalog
from local.storage import atomic_json, output_lock


def main() -> int:
    """按 --config/--item 选择当前清单的一集；--report-dir 保存脱敏结果，--full-download 验收全片及复用；返回退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--item", type=int, required=True, help="当前清单条目的零基序号")
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--full-download", action="store_true", help="额外执行单集全片下载、完整解码、发布及第二次复用")
    args = parser.parse_args()
    config = load_config(args.config, source="url")
    values = requests(config)
    if not 0 <= args.item < len(values):
        parser.error("--item 超出当前清单范围")
    request = replace(values[args.item], all_parts=False)
    client = NativeClient(config)
    task = catalog(client, request)[0]
    initial = client.source(task["series_id"], task["video_id"])
    selected = resolve(client, task, AudioSelector(config["audio"]))
    refreshed = resolve(client, task, AudioSelector(config["audio"]), expected=selected)
    report = {"task": task, "login_library_version": client.user["app_version"],
              "signature_adapter_version": client.profile["playback"]["ckey"]["client_version"],
              "playback_app_version": client.profile["playback"]["app_version"],
              "playback_protocol": client.profile["playback"]["query"]["encryptVer"],
              "formats": [{key: value.get(key) for key in ("id", "name", "sname", "width", "height", "vfps", "lmt")}
                          for value in formats(initial, client.profile["playback"])],
              "selected": selected.description, "identity": selected.identity,
              "refreshed_identity_matches": refreshed.identity == selected.identity,
              "resources": sum(len(item.resources) for item in selected.manifests),
              "manifest_durations": [item.duration for item in selected.manifests]}
    code = 0
    if args.full_download:
        with output_lock(config["output_path"]), output_lock(config["work_dir"] / "url"):
            journal = Journal(config)
            try:
                first = UrlBatch(config, journal, client=client).run([task])
                second = UrlBatch(config, journal, client=client).run([task]) if not first["failed"] else {}
                report["download_runs"] = [first, second]
                code = int(first["failed"] > 0 or second.get("reused") != 1)
                report["entries"] = journal.run["entries"]
            finally:
                journal.close("failed" if code else "completed", {"failed": code})
    args.report_dir.mkdir(parents=True, exist_ok=True)
    output = args.report_dir / f"verify_playback_{uuid4().hex}.json"
    atomic_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"验收报告：{output}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
