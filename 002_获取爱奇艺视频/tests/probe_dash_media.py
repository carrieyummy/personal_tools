"""通过真实已登录 SDK 验证目标集的内嵌 DASH 与 CENC 短样本；不切换界面或创建离线下载任务。"""

import argparse
from datetime import datetime
import json
import re
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.native import NativeV5
from qsv_local.playback.configuration import read_profile
from qsv_local.playback.listing import expand_episodes, task_options
from qsv_local.playback.manifests import parse_media_manifest
from qsv_local.playback.media import fetch_segment, media_config
from qsv_local.playback.metadata import linked_videos, public_video
from qsv_local.playback.mp4 import boxes, fragment, initialization
from qsv_local.playback.source import select_source
from qsv_local.playback.network import open_network


def main() -> int:
    """解析实际配置、链接下标及集序；可获取短样本验证原生 CENC，成功返回 0，失败返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="实际播放专项配置")
    parser.add_argument("--index", required=True, type=int, help="链接清单下标")
    parser.add_argument("--episode", required=True, type=int, help="该专辑中的正整数集序")
    parser.add_argument("--tvid", help="可选：直接使用已知正片 TVID 查询公开资料，避免重复短链跳转")
    parser.add_argument("--metadata-report", type=Path, help="可选：读取既有批次的目标元数据，避免短样本研究反复查询公开资料")
    parser.add_argument("--sample-media", action="store_true", help="获取各轨初始化和第一个媒体片段，处理授权后解码验证")
    parser.add_argument("--inspect-network", action="store_true", help="仅检查原始初始化地址与 MPD 范围声明，不获取整片")
    args = parser.parse_args()
    profile = read_profile(args.config)
    if args.metadata_report:
        previous = json.loads(args.metadata_report.read_text(encoding="utf-8"))
        video = next(job["video"] for job in previous["jobs"] if job["video"]["index"] == args.index and job["video"]["episode"] == args.episode)
        if args.tvid and video["tvid"] != args.tvid:
            raise ValueError("既有批次的 TVID 与指定目标不一致")
    elif args.tvid:
        video = public_video(profile, args.tvid, args.index)
        if video["episode"] != args.episode:
            raise ValueError("TVID 与请求集序不一致")
    else:
        link = linked_videos(profile, [args.index])[0]
        video = next(row for row in expand_episodes(profile, link, True) if row["episode"] == args.episode)
    result = select_source(profile, video, task_options(profile)[args.index]["quality"])
    plan = parse_media_manifest(result.manifest, result.selected, profile)
    print(json.dumps({"selection": result.report["selection"], "format": plan["format"], "tracks": plan["tracks"],
                      "duration": plan["duration"], "segments": len(plan["segments"])}, ensure_ascii=False), flush=True)
    if args.inspect_network:
        xml = ET.fromstring(result.manifest)
        for item in xml.iter():
            kind = item.tag.rsplit("}", 1)[-1]
            if kind not in ("Initialization", "SegmentURL"):
                continue
            url = item.get("sourceURL") or item.get("media")
            if not url:
                continue
            parsed = urlsplit(url)
            print(json.dumps({"kind": kind, "attribute_names": sorted(item.attrib), "range": item.get("range") or item.get("mediaRange"),
                              "host": parsed.hostname, "query_fields": sorted(parse_qs(parsed.query)), "suffix": Path(parsed.path).suffix}), flush=True)
            if kind == "Initialization":
                headers = dict(result.headers)
                bounds = item.get("range")
                if bounds:
                    headers["Range"] = "bytes=" + bounds
                try:
                    with open_network(profile, Request(url, headers=headers)) as response:
                        data = response.read(profile["playback"]["media_probe"]["max_segment_bytes"] + 1)
                        text = data.decode("utf-8", "replace").strip()
                        embedded = None
                        if "{" in text:
                            try:
                                embedded, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
                            except ValueError:
                                pass
                        safe_syntax = re.sub(r'''"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*' ''', '"[literal]"', text, flags=re.X)
                        safe_syntax = re.sub(r"https?[^\s<>]+", "[address]", safe_syntax)
                        print(json.dumps({"status": response.status, "content_type": response.headers.get("Content-Type"),
                                          "content_encoding": response.headers.get("Content-Encoding"), "magic": data[:3].hex(),
                                          "content_length": response.headers.get("Content-Length"), "content_range": response.headers.get("Content-Range"),
                                          "actual_bytes": len(data), "json_fields": sorted(json.loads(data)) if data.lstrip().startswith(b"{") else None,
                                          "embedded_json_fields": sorted(embedded) if isinstance(embedded, dict) else None,
                                          "text_is_url": urlsplit(text.strip('"')).scheme in ("http", "https"),
                                          "text_is_html": text.lower().startswith(("<!doctype", "<html")),
                                          "text_is_xml": text.startswith("<?xml"),
                                          "response_host": urlsplit(response.url).hostname,
                                          "safe_syntax": safe_syntax[:2048] if text.startswith("var ") else None,
                                          "box_kind": data[4:8].decode("ascii", "replace") if len(data) >= 8 and data[4:8] in (b"ftyp", b"styp", b"moov") else None}), flush=True)
                except Exception as error:
                    print(json.dumps({"network_error": type(error).__name__, "reason_type": type(getattr(error, "reason", None)).__name__}), flush=True)
            break
        return 0
    if not args.sample_media:
        return 0
    if plan["format"] != "dash":
        raise ValueError("本探测只处理 DASH 短样本")
    config = media_config(profile)
    root = profile["report_dir"] / ("dash_sample_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f"))
    root.mkdir(parents=True, exist_ok=False)
    with NativeV5(config["sdk"], result.selected["drm"]["ticket"].encode("ascii")) as sdk:
        sdk.configure_cenc()
        for index, track in enumerate(plan["tracks"]):
            rows = [row for row in plan["segments"] if row["track"] == index]
            positions = profile["playback"]["media_probe"]["positions"]
            sample_index = 1 + round(positions[len(positions) // 2] * (len(rows) - 2))
            sample_row = rows[sample_index]
            print(json.dumps({"track": index, "sample_index": sample_index, "sample_ranges": [{key: parse_qs(urlsplit(row["url"]).query).get(key) for key in ("start", "end", "contentlength")} for row in (rows[0], sample_row)]}), flush=True)
            initial, _ = fetch_segment(profile, rows[0]["url"], result.headers, profile["playback"]["media_probe"]["max_segment_bytes"])
            raw, _ = fetch_segment(profile, sample_row["url"], result.headers, profile["playback"]["media_probe"]["max_segment_bytes"])
            (root / f"{index:02d}_initialization.mp4").write_bytes(initial)
            (root / f"{index:02d}_encrypted.m4s").write_bytes(raw)
            print(json.dumps({"track": index, "init_boxes": [b[2].decode("ascii") for b in boxes(initial)],
                              "media_boxes": [b[2].decode("ascii") for b in boxes(raw)], "bytes": [len(initial), len(raw)]}), flush=True)
            clear_init, track_info = initialization(initial)
            print(json.dumps({"track_info": track_info}), flush=True)
            offset = int(parse_qs(urlsplit(sample_row["url"]).query)["start"][0])
            clear, statistics = fragment(raw, sdk, track_info, offset)
            path = root / (track["kind"] + ".mp4")
            path.write_bytes(clear_init + clear)
            command = [str(config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode", "-i", str(path), "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"]
            process = subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL, timeout=config["process_timeout_seconds"],
                                     creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            (root / (track["kind"] + "_decode.log")).write_bytes(process.stderr)
            print(json.dumps({"track": index, "statistics": statistics, "decode_exit_code": process.returncode, "work_dir": str(root)}), flush=True)
            if process.returncode:
                print(process.stderr.decode("utf-8", "replace"), flush=True)
                return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"DASH 样本验证失败：{type(error).__name__}：{error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
