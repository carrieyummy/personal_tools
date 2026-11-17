"""研究精确同源码流文件授权键及标准 Range 传输，不修改画质或保存密钥/签名地址。"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.configuration import load_config, resolve_path
from local.playback.client import NativeClient, formats, video_info
from research.settings import sample_requests as requests
from local.playback.hls import parse_media
from local.playback.transport import Transport, safe_message
from local.storage import atomic_json


def main() -> None:
    """按 --config 的研究条目请求同文件 getkey，再有界读取同源码流前缀，保存脱敏元数据报告。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    path = args.config.resolve()
    research = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    config = load_config(resolve_path(research["config"], path.parent), source="url")
    client = NativeClient(config)
    probe = deepcopy(config)
    probe["network"]["timeout_seconds"] = research["transfer_probe"]["timeout_seconds"]
    transport = Transport(probe)
    report = []
    for request in requests(config):
        initial = client.source(request.series_id, request.video_id)
        choice = formats(initial, client.profile["playback"])[request.quality - 1]
        video = video_info(client.source(request.series_id, request.video_id, choice["name"]))
        usable = [mirror for mirror in video["ul"]["ui"] if mirror["vt"] not in research["transfer_probe"]["excluded_delivery_types"]]
        first_media = parse_media(transport.text(usable[0]["url"]), usable[0]["url"])
        physical = next(value for value in first_media.resources if not value.initial)
        filename = Path(urlsplit(physical.url).path).name
        key = client.raw_key(request.series_id, request.video_id, filename, choice["id"])
        entry = {"note": request.note, "format_id": choice["id"], "filename": filename,
                 "logical_filename": video["fn"], "file_count": video["fc"],
                 "video_fields": sorted(video), "clip_info": {"fields": sorted(video.get("cl", {})),
                     "clips": [{name: value[name] for name in ("keyid", "idx", "cd", "cs", "cmd5") if name in value}
                               for value in video.get("cl", {}).get("ci", [])]},
                 "returned_filename": key.get("filename"), "returned_keyid": key.get("keyid"),
                 "key_fields": sorted(key), "code": key.get("code"), "em": key.get("em"), "exem": key.get("exem"),
                 "filename_matches": key.get("filename") == filename, "has_key": bool(key.get("key")),
                 "file_size": video["fs"], "cdns": []}
        canonical = key.get("filename")
        if entry["has_key"] and isinstance(canonical, str) and Path(canonical).name == canonical:
            for mirror in usable[:1]:
                playlist = parse_media(transport.text(mirror["url"]), mirror["url"])
                resource = next(value for value in playlist.resources if not value.initial)
                parsed = urlsplit(resource.url)
                # getkey 的 keyid 给出物理分段编号，比较官方文件名、对应物理段和 HLS 虚拟段。
                suffix = str(key.get("keyid", "")).rsplit(".", 1)[-1]
                candidates = {"canonical": canonical, "virtual": filename}
                if suffix.isdecimal():
                    stem, extension = canonical.rsplit(".", 1)
                    candidates["keyid_segment"] = f"{stem}.{suffix}.{extension}"
                # 腾讯 CDN 的路径本身也含授权签名，不能将路径写入报告。
                entry["manifest_basename"] = urlsplit(mirror["url"]).path.rsplit("/", 1)[-1]
                entry["resource_basename"] = parsed.path.rsplit("/", 1)[-1]
                for kind, candidate in candidates.items():
                    canonical_path = parsed.path.rsplit("/", 1)[0] + "/" + candidate
                    url = urlunsplit((parsed.scheme, parsed.netloc, canonical_path, urlencode({"vkey": key["key"],
                          "sdtfrom": config["playback"]["playback"]["query"]["sdtfrom"]}), ""))
                    sample = research["transfer_probe"]["sample_bytes"]
                    result, started, count = {"host": urlsplit(url).hostname, "kind": kind}, time.monotonic(), 0
                    try:
                        with transport.open(url, {"Range": f"bytes=0-{sample - 1}"}) as response:
                            result.update(status=response.code, headers={name: response.headers.get(name) for name in
                                          ("Content-Type", "Content-Length", "Content-Range")})
                            prefix = b""
                            while count < sample:
                                if time.monotonic() - started > probe["network"]["timeout_seconds"]:
                                    raise TimeoutError("授权文件小范围读取超时")
                                block = response.read1(sample - count)
                                if not block:
                                    break
                                prefix += block[:max(0, 4 - len(prefix))]
                                count += len(block)
                            result["prefix_hex"] = prefix.hex()
                    except Exception as error:
                        result["error"] = safe_message(error)
                    result.update(bytes=count, seconds=round(time.monotonic() - started, 3))
                    entry["cdns"].append(result)
        report.append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
    target = resolve_path(research["report_dir"], path.parent) / "file_key_probe.json"
    atomic_json(target, {"items": report})
    print(f"同源授权文件研究报告：{target}")


if __name__ == "__main__":
    main()
