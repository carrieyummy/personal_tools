"""比较同一最高档来源的小范围 HTTP 传输；签名地址经内存传递，媒体样本不落盘。"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.configuration import load_config, resolve_path
from local.playback.client import NativeClient, formats, video_info
from local.playback.hls import parse_media
from local.playback.tasks import requests
from local.playback.transport import Transport, safe_message
from local.storage import atomic_json


def curl_quote(value: str) -> str:
    """对内存 curl 配置的 value 进行引号与反斜杠转义；返回不含换行的配置字符串。"""
    if "\n" in value or "\r" in value:
        raise ValueError("curl 配置值不允许换行")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def main() -> None:
    """按 --config 选择当前研究条目，对各标准 CDN 执行有界小范围读取并写脱敏报告。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    path = args.config.resolve()
    research = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    config = load_config(resolve_path(research["config"], path.parent), source="url")
    settings = research["transfer_probe"]
    probe_config = deepcopy(config)
    probe_config["network"]["timeout_seconds"] = settings["timeout_seconds"]
    transport, client = Transport(probe_config), NativeClient(config)
    entries = []
    for item in requests(config):
        if research["title"] and item.note != research["title"]:
            continue
        initial = client.source(item.series_id, item.video_id)
        choice = formats(initial, client.profile["playback"])[item.quality - 1]
        video = video_info(client.source(item.series_id, item.video_id, choice["name"]))
        for mirror in video["ul"]["ui"]:
            if mirror["vt"] in settings["excluded_delivery_types"]:
                continue
            base = {"host": urlsplit(mirror["url"]).hostname, "format_id": choice["id"], "delivery_type": mirror["vt"]}
            try:
                media = parse_media(transport.text(mirror["url"]), mirror["url"])
            except Exception as error:
                entry = {**base, "phase": "manifest", "error": safe_message(error)}
                entries.append(entry)
                print(json.dumps(entry, ensure_ascii=False), flush=True)
                continue
            resource = next(value for value in media.resources if not value.initial)
            limit = min(settings["sample_bytes"], resource.expected_bytes or settings["sample_bytes"])
            started, count = time.monotonic(), 0
            entry = {**base, "transport": "urllib", "resource_host": urlsplit(resource.url).hostname}
            try:
                with transport.open(resource.url, {"Range": f"bytes=0-{limit - 1}"}) as response:
                    entry.update(status=response.code, headers={key: response.headers.get(key) for key in
                                 ("Content-Type", "Content-Length", "Content-Range")})
                    while count < limit:
                        if time.monotonic() - started > settings["timeout_seconds"]:
                            raise TimeoutError("小范围读取超过研究总超时")
                        block = response.read1(limit - count)
                        if not block:
                            break
                        count += len(block)
                entry["bytes"] = count
            except Exception as error:
                entry.update(error=safe_message(error), bytes=count)
            entry["seconds"] = round(time.monotonic() - started, 3)
            entries.append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
            curl_config = '\n'.join([f'url = "{curl_quote(resource.url)}"',
                *(f'header = "{curl_quote(key + ": " + value)}"' for key, value in config["network"]["headers"].items())])
            command = [str(resolve_path(settings["curl_path"], path.parent)), "--silent", "--show-error", "--fail",
                       "--location", "--max-time", str(settings["timeout_seconds"]), "--proxy", config["network"]["proxy"],
                       "--range", f"0-{limit - 1}", "--max-filesize", str(resource.expected_bytes or limit),
                       "--write-out", "\n%{http_code}", "--config", "-"]
            started = time.monotonic()
            try:
                result = subprocess.run(command, input=curl_config.encode("utf-8"), capture_output=True,
                                        timeout=settings["timeout_seconds"] + config["runtime"]["process_terminate_timeout_seconds"],
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                body, _, status = result.stdout.rpartition(b"\n")
                entry = {**base, "transport": "curl", "returncode": result.returncode, "status": status.decode(errors="replace"),
                         "bytes": len(body), "prefix_hex": body[:4].hex(), "seconds": round(time.monotonic() - started, 3)}
            except Exception as error:
                entry = {**base, "transport": "curl", "error": safe_message(error)}
            entries.append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    output = resolve_path(research["report_dir"], path.parent) / "transfer_probe.json"
    atomic_json(output, {"entries": entries})


if __name__ == "__main__":
    main()
