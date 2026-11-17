"""研究官方播放接口的不同交付模式；仅探测相同画质，所有授权地址和媒体样本留在内存。"""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.configuration import load_config, resolve_path
from local.playback.client import NativeClient, formats, video_info
from local.playback.hls import parse_media
from local.playback.source import selected_format
from research.settings import sample_requests as requests
from local.playback.transport import Transport, safe_message
from local.storage import atomic_json


def main() -> None:
    """读取 --config 的交付模式与探测边界；比较精确同档来源的前缀吞吐，保存脱敏报告，无返回值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--variant", action="append", help="可选：只运行指定名称，可重复传入；省略时运行专项配置全部模式")
    args = parser.parse_args()
    path = args.config.resolve()
    research = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    config = load_config(resolve_path(research["config"], path.parent), source="url")
    settings = research["transfer_probe"]
    probe_config = deepcopy(config)
    probe_config["network"]["timeout_seconds"] = settings["timeout_seconds"]
    transport, client = Transport(probe_config), NativeClient(config)
    profile = client.profile["playback"]
    baseline, baseline_network, entries = deepcopy(profile), deepcopy(config["network"]), []
    selected_names = set(args.variant or [])
    known_names = {variant["name"] for variant in settings["delivery_variants"]}
    if selected_names - known_names:
        parser.error("--variant 包含专项配置中不存在的名称")
    for item in requests(config):
        profile.clear()
        profile.update(deepcopy(baseline))
        config["network"] = deepcopy(baseline_network)
        client.transport.network = config["network"]
        choice = formats(client.source(item.series_id, item.video_id), client.profile["playback"])[item.quality - 1]
        for variant in settings["delivery_variants"]:
            if selected_names and variant["name"] not in selected_names:
                continue
            entry = {"note": item.note, "variant": variant["name"], "format_id": choice["id"]}
            count, started = 0, time.monotonic()
            try:
                profile["query"] = deepcopy(baseline["query"])
                profile["app_version"] = client.user["app_version"] if variant.get("native_app_version") else baseline["app_version"]
                profile["endpoint"] = variant.get("endpoint", baseline["endpoint"])
                profile["request_mode"] = variant.get("request_mode", baseline["request_mode"])
                network = deepcopy(baseline_network)
                network["headers"].update(variant.get("headers", {}))
                if variant.get("proxy_environment"):
                    network["proxy"] = os.environ[variant["proxy_environment"]]
                config["network"] = client.transport.network = network
                transport.network = {**network, "timeout_seconds": settings["timeout_seconds"]}
                for name in variant.get("remove_query", []):
                    client.profile["playback"]["query"].pop(name, None)
                data = client.source(item.series_id, item.video_id, choice["name"], variant["query"])
                selected_format(data, choice, config["verification"]["fps_tolerance"])
                video = video_info(data)
                entry.update(dltype=data.get("dltype"), preview=data.get("preview"), exem=data.get("exem"),
                    video={name: video.get(name) for name in ("fn", "st", "fst", "fs", "td", "fc", "drm", "enc", "encryption")},
                    formats=[{name: value.get(name) for name in ("name", "dummy", "preview", "lmt", "sl")} for value in data["fl"]["fi"]])
                priority = client.profile["playback"]["delivery_priority"]
                mirrors = sorted(video["ul"]["ui"], key=lambda value: priority.index(value["vt"]) if value["vt"] in priority else len(priority))
                mirror = next(value for value in mirrors if value["vt"] not in settings["excluded_delivery_types"])
                address = mirror["url"] + mirror.get("hls", {}).get("pt", "")
                entry["manifest_query_names"] = sorted({name for name, _ in parse_qsl(urlsplit(address).query)})
                if data.get("dltype") in (3, 8) or ".m3u8" in urlsplit(address).path:
                    playlist = parse_media(transport.text(address), address)
                    resource = next(value for value in playlist.resources if not value.initial)
                    address = resource.url
                elif video.get("fvkey"):
                    # 与公开播放器 vf 的目录+fn、sdtfrom/guid/vkey 构造一致；不猜测受签名保护的路径。
                    if not urlsplit(address).path.endswith((".mp4", ".flv", ".ts")):
                        address += video["fn"]
                    address += ("&" if "?" in address else "?") + urlencode({
                        "sdtfrom": baseline["query"]["sdtfrom"], "vkey": video["fvkey"]})
                else:
                    raise ValueError("未返回可验证的 HLS 或直连文件授权")
                parts = urlsplit(address)
                media_query = dict(parse_qsl(parts.query))
                entry["resource_query_names"] = sorted(media_query)
                for name in variant.get("media_context", []):
                    media_query.setdefault(name, client.profile["playback"]["query"][name])
                if variant.get("media_keyid"):
                    media_query.setdefault("keyid", video["keyid"])
                if variant.get("media_context") or variant.get("media_keyid"):
                    address = urlunsplit(parts._replace(query=urlencode(media_query)))
                entry["host"] = urlsplit(address).hostname
                sample = settings["sample_bytes"]
                started = time.monotonic()
                media_headers = {"Range": f"bytes=0-{sample - 1}"}
                if variant.get("media_cookie"):
                    media_headers["Cookie"] = "; ".join(f"{name}={client.user['tokens'][field]}"
                                                       for name, field in variant["media_cookie"].items())
                with transport.open(address, media_headers) as response:
                    entry.update(status=response.code, header_names=sorted(response.headers), headers={name: response.headers.get(name) for name in
                        ("Content-Type", "Content-Length", "Content-Range")})
                    prefix = b""
                    while count < sample:
                        if time.monotonic() - started > settings["timeout_seconds"]:
                            raise TimeoutError("交付模式探测达到总超时")
                        block = response.read1(sample - count)
                        if not block:
                            break
                        prefix += block[:max(0, 4 - len(prefix))]
                        count += len(block)
                    entry["prefix_hex"] = prefix.hex()
            except Exception as error:
                entry["error"] = safe_message(error)
            entry.update(bytes=count, seconds=round(time.monotonic() - started, 3))
            entries.append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    target = resolve_path(research["report_dir"], path.parent) / "delivery_variants.json"
    atomic_json(target, {"entries": entries})
    print(f"交付模式研究报告：{target}")


if __name__ == "__main__":
    main()
