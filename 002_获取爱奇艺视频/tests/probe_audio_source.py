"""仅查询原生最高音轨的字段和资源形状，不输出地址或授权，不下载媒体。"""

import argparse
import json
from pathlib import Path
import sys
from datetime import datetime
import subprocess
import gzip
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.playback.audio import audio_groups, audio_segments, select_native_audio
from qsv_local.playback.configuration import read_profile
from qsv_local.playback.listing import expand_episodes, task_options
from qsv_local.playback.media import fetch_segment, media_config, save_segment
from qsv_local.playback.coordination import StageControls
from qsv_local.playback.processing import process_audio_assets
from qsv_local.playback.metadata import linked_videos, public_video
from qsv_local.playback.source import select_source
from qsv_local.playback.source import request_source


def audio_shape(payload: dict) -> list:
    """提取 payload 的音轨资源类型、字段名及公开档位；返回不含地址或票据内容的结构。"""
    return [{"scalar": {key: row[key] for key in ("aid", "bid", "ct", "cf", "aat", "mver", "name", "ff", "asize", "_selected", "_match") if key in row},
             "native_scalars": {key: row[key] for key in ("s", "t", "amt", "ctype", "lid", "_sort", "_match") if key in row},
             "shape": {key: {"type": type(value).__name__, "length": len(value) if isinstance(value, (dict, list, str)) else None,
                             "fields": sorted(value) if isinstance(value, dict) else sorted(value[0]) if isinstance(value, list) and value and isinstance(value[0], dict) else None}
                       for key, value in row.items()}} for row in payload.get("data", {}).get("program", {}).get("audio", [])]


def resource_shape(value, depth: int = 6):
    """递归描述 value 的资源类型；depth 限定层数、列表只观察首项，字符串仅显示URL结构或长度，返回安全字典。"""
    if isinstance(value, str):
        parsed = urlsplit(value)
        return {"type": "str", "length": len(value), "header_name": value.partition(":")[0] if value.partition(":")[0].casefold() in ("user-agent", "referer") else None,
                "uri_shape": {"scheme": parsed.scheme, "host": parsed.hostname,
                "suffix": Path(parsed.path).suffix, "query_fields": sorted(parse_qs(parsed.query))} if parsed.scheme in ("http", "https") or value.startswith("/") else None}
    if isinstance(value, dict):
        return {key: resource_shape(child, depth - 1) for key, child in value.items()} if depth else {"fields": sorted(value)}
    if isinstance(value, list):
        return {"type": "list", "count": len(value), "first": resource_shape(value[0], depth - 1) if value and depth else None}
    return {"type": type(value).__name__}


def main() -> int:
    """读取配置、链接及集序并查询指定排名音轨，输出白名单字段；成功返回 0。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="实际播放专项配置")
    parser.add_argument("--index", required=True, type=int, help="实际清单下标")
    parser.add_argument("--episode", required=True, type=int, help="专辑内的正整数集序")
    parser.add_argument("--tvid", help="可选：直接查询已知正片编号，避免重复短链跳转")
    parser.add_argument("--metadata-report", type=Path, help="可选：复用既有批次的公开目标资料")
    parser.add_argument("--request-audio-type", type=int, help="可选：仅探测原生音频编码枚举")
    parser.add_argument("--request-application-type", type=int, help="可选：仅探测原生音轨应用类型")
    parser.add_argument("--sample-media", action="store_true", help="可选：仅获取最高音轨初始化/首片结构，用于适配验证")
    parser.add_argument("--full-media", action="store_true", help="可选：完整获取最高独立音轨、还原并解码验证，不发布正式成品")
    args = parser.parse_args()
    profile = read_profile(args.config)
    if args.metadata_report:
        previous = json.loads(args.metadata_report.read_text(encoding="utf-8"))
        video = next(job["video"] for job in previous["jobs"] if job["video"]["index"] == args.index and job["video"]["episode"] == args.episode)
        if args.tvid and video["tvid"] != args.tvid:
            raise ValueError("既有批次的 TVID 与指定目标不一致")
    else:
        video = public_video(profile, args.tvid, args.index) if args.tvid else next(row for row in expand_episodes(profile, linked_videos(profile, [args.index])[0], True) if row["episode"] == args.episode)
    if video["episode"] != args.episode:
        raise ValueError("TVID 与请求集序不一致")
    source = select_source(profile, video, task_options(profile)[args.index]["quality"])
    print(json.dumps({"initial_audio": audio_shape(source.payload)}, ensure_ascii=False), flush=True)
    print(json.dumps({"initial_request_selection": source.report["request_selection"]}), flush=True)
    print(json.dumps({"dispatch_metadata": {key: resource_shape(value) for key, value in source.payload.get("data", {}).items() if key in ("dm", "dm3u8", "dd", "stl")}}), flush=True)
    if args.request_audio_type is not None or args.request_application_type is not None:
        desired = audio_groups(source.payload["data"]["program"]["audio"], profile["playback"]["audio_selection"])[0][0]
        override = {"bitstream": desired["bid"], "audio_channel_type": desired["ct"], "audio_language": desired["lid"],
                    "audio_type": args.request_audio_type if args.request_audio_type is not None else profile["playback"]["request"]["audio_type"],
                    "audio_extend_info": {"abid": desired["bid"], "application_type": args.request_application_type if args.request_application_type is not None else desired["aat"]}}
        queried = request_source(profile, video, override, require_video=False)
        print(json.dumps({"experimental_selection": queried.report["request_selection"], "audio": audio_shape(queried.payload)}, ensure_ascii=False), flush=True)
        return 0
    if source.manifest.lstrip().startswith(b"<"):
        root = ET.fromstring(source.manifest)
        candidates = source.payload["data"]["program"].get("audio", [])
        for adaptation in root.iter():
            if adaptation.tag.rsplit("}", 1)[-1] != "AdaptationSet" or adaptation.get("contentType") != "audio":
                continue
            for element in adaptation.iter():
                url = element.get("sourceURL") or element.get("media")
                if url:
                    query = parse_qs(urlsplit(url).query)
                    matches = [row["aid"] for row in candidates if row["aid"] in urlsplit(url).path or row["aid"] in [v for values in query.values() for v in values]]
                    print(json.dumps({"mpd_audio_query_fields": sorted(query), "matching_aids": matches,
                                      "representation_id": adaptation.find("{urn:mpeg:dash:schema:mpd:2011}Representation").get("id")}), flush=True)
                    break

    def inspect_request(*values, **options):
        """调用实际 SDK 的 values/options 请求并打印音轨白名单结构；返回原资源对象。"""
        result = request_source(*values, **options)
        print(json.dumps({"requested_audio": audio_shape(result.payload)}, ensure_ascii=False), flush=True)
        print(json.dumps({"requested_selection": result.report["request_selection"]}), flush=True)
        return result

    with patch("qsv_local.playback.source.request_source", side_effect=inspect_request):
        selected = select_native_audio(profile, video, source, media_config(profile)["audio"]["quality"])
    shape = {key: {"type": type(value).__name__, "length": len(value) if isinstance(value, (list, dict, str)) else None,
                   "fields": sorted(value) if isinstance(value, dict) else sorted(value[0]) if isinstance(value, list) and value and isinstance(value[0], dict) else None}
             for key, value in selected.selected.items()}
    print(json.dumps({"selection": selected.report, "shape": shape,
                      "scalar": {key: selected.selected[key] for key in ("bid", "ct", "cf", "aat", "mver", "name", "ff", "asize") if key in selected.selected}}, ensure_ascii=False), flush=True)
    print(json.dumps({"fs": [{key: row[key] for key in ("b", "d", "i") if key in row} for row in selected.selected["fs"]],
                      "fs_shapes": [{"scheme": urlsplit(row["l"]).scheme, "host": urlsplit(row["l"]).hostname,
                                     "suffix": Path(urlsplit(row["l"]).path).suffix, "query_fields": sorted(parse_qs(urlsplit(row["l"]).query)),
                                     "play_type": type(row.get("play")).__name__, "play_fields": sorted(row["play"]) if isinstance(row.get("play"), dict) else None}
                                    for row in selected.selected["fs"][:2]]}), flush=True)
    print(json.dumps({"first_fs_play_structure": resource_shape(selected.selected["fs"][0].get("play"))}), flush=True)
    print(json.dumps({"public_audio_headers": {key: value for key, value in selected.selected["fs"][0].get("play", {}).get("ts", {}).items() if key == "ua"},
                      "path_prefix": urlsplit(selected.selected["fs"][0]["l"]).path.split("/")[:3],
                      "audio_dispatch_metadata": {key: resource_shape(value) for key, value in selected.payload.get("data", {}).items() if key in ("dm", "dm3u8", "dd", "stl")}}), flush=True)
    if args.sample_media or args.full_media:
        config = media_config(profile)
        segments = audio_segments(profile, selected.selected, video["duration_seconds"], selected.payload["data"].get("dd"))
        work = profile["report_dir"] / ("audio_sample_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f"))
        work.mkdir(parents=True, exist_ok=False)
        paths = []
        for index, row in enumerate(segments if args.full_media else segments[:2]):
            path = work / f"{index:04d}.amp4"
            if args.full_media:
                save_segment(profile, row["url"], row["headers"], path, row["end"] - row["start"], config["copy_buffer_bytes"], range_style=row["range_style"])
            else:
                raw, _ = fetch_segment(profile, row["url"], row["headers"], profile["playback"]["media_probe"]["max_segment_bytes"], range_style=row["range_style"])
                path.write_bytes(raw)
            paths.append(path)
            with path.open("rb") as stream:
                prefix = stream.read(12)
            print(json.dumps({"part": index, "bytes": path.stat().st_size, "gzip": prefix[:2] == b"\x1f\x8b", "box_type": prefix[4:8].decode("ascii", "replace") if prefix[4:8] in (b"ftyp", b"moov", b"mdat", b"moof", b"styp") else None}), flush=True)
        if args.full_media:
            target = process_audio_assets(profile, config, paths, work, StageControls(config), lambda done, total: None)
            command = [str(config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-xerror", "-err_detect", "explode", "-i", str(target), "-map", "0:a", "-f", "null", "-"]
            process = subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL, timeout=config["process_timeout_seconds"], creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            (work / "audio_decode.log").write_bytes(process.stderr)
            print(json.dumps({"work_dir": str(work), "decode_exit_code": process.returncode}), flush=True)
            for line in process.stderr.decode("utf-8", "replace").splitlines():
                if "Duration:" in line or "Audio:" in line or "Error" in line or "Invalid" in line:
                    print(line, flush=True)
            return 0 if process.returncode == 0 else 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"音轨探测失败：{type(error).__name__}：{error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
