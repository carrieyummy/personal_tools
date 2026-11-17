"""读取当前清单和原生登录态，生成不含票据/签名地址的画质与音质接口研究报告。"""

import argparse
from datetime import datetime
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.configuration import load_config, resolve_path
from local.playback.client import NativeClient, video_info
from research.settings import sample_requests as requests
from local.playback.transport import safe_message


def audio_schema(value, *, depth: int = 0):
    """递归描述音频 value 的类型、字段与公开能力标量；depth 限制展开；返回不包含地址、票据或不透明载荷的结构。"""
    if depth > 5:
        return {"type": type(value).__name__}
    if isinstance(value, dict):
        public = {"id", "name", "sname", "lang", "language", "codec", "aformat", "format", "acodec", "br", "bitrate",
                  "bandwidth", "audiobandwidth", "channels", "channel", "sample_rate", "samplerate", "vip", "isvip",
                  "lmt", "sl", "drm", "pay", "preview", "label", "role", "type", "af", "at", "ac", "mt", "quality",
                  "audio", "track", "feature", "action", "control", "audioscore", "fs", "bname", "cname"}
        parameters = {key: nested for key, nested in value.items() if key in public and
                      (type(nested) in (int, float, bool) or isinstance(nested, str) and len(nested) <= 80
                       and not nested.startswith(("http:", "https:")) and "=" not in nested)}
        return {"type": "object", "fields": sorted(value), "parameters": parameters,
                "nested": {key: audio_schema(nested, depth=depth + 1) for key, nested in value.items() if isinstance(nested, (dict, list))}}
    if isinstance(value, list):
        return {"type": "list", "items": [audio_schema(nested, depth=depth + 1) for nested in value]}
    return {"type": type(value).__name__, "length": len(value) if isinstance(value, str) else None}


def describe(data: dict) -> dict:
    """将原生播放 data 缩减为脱敏协议结构和真实档位；返回可落盘对象，不保存完整响应。"""
    result = {"code": data.get("code"), "em": data.get("em"), "exem": data.get("exem"),
              "login": data.get("login"), "preview": data.get("preview"), "fields": sorted(data)}
    fields = ("id", "name", "br", "sname", "resolution", "width", "height", "vfps", "drm", "lmt",
              "sl", "dummy", "preview", "bandwidth", "audiobandwidth", "group", "formatdefn", "videoenh", "zqe", "audioscore")
    result["formats"] = [{key: value[key] for key in fields if key in value} for value in data.get("fl", {}).get("fi", [])]
    if data.get("vl"):
        video = video_info(data)
        result["video"] = {key: video.get(key) for key in ("br", "vw", "vh", "fps", "td", "drm", "enc", "st", "fst", "totalduration")}
        result["video_fields"] = sorted(video)
        result["audio_fields"] = {key: {"type": type(value).__name__, "fields": sorted(value) if isinstance(value, dict) else None}
                                  for key, value in {**data, **video}.items() if "audio" in key.lower() or key in ("al", "afl")}
        result["audio_schema"] = {key: audio_schema(value) for key, value in {**data, **video}.items()
                                  if "audio" in key.lower() or key in ("al", "afl")}
        result["audio_bindings"] = [{"filename_empty": not item.get("filename"), "filename_matches_video": item.get("filename") == video.get("fn"),
                                     "keyid_empty": not item.get("keyid"), "keyid_matches_video": item.get("keyid") == video.get("keyid"),
                                     "id_matches_selected_video": item.get("id") in {fmt.get("id") for fmt in data.get("fl", {}).get("fi", []) if fmt.get("sl") == 1}}
                                    for item in data.get("al", {}).get("ai", [])]
        result["delivery_fields"] = [{"fields": sorted(value), "hls_fields": sorted(value.get("hls", {}))}
                                      for value in video.get("ul", {}).get("ui", [])]
    return result


def main() -> None:
    """解析必填研究 --config，查询当前清单和各能力变体；打印脱敏摘要并保存独立报告。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    path = args.config.resolve()
    research = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    config = load_config(resolve_path(research["config"], path.parent), source="url")
    client = NativeClient(config)
    baseline = deepcopy(client.profile["playback"])
    report = {"login_library_version": client.user["app_version"], "playback_sdk_version": baseline["app_version"],
              "signature_adapter_version": baseline["ckey"]["client_version"], "items": []}
    for item in requests(config):
        series_id, video_id = item.series_id, item.video_id
        entry = {"note": item.note, "series_id": series_id, "video_id": video_id, "variants": []}
        for variant in research["variants"]:
            result = {"name": variant["name"], "query": variant["query"]}
            try:
                client.profile["playback"]["query"] = deepcopy(baseline["query"])
                for key in variant.get("remove_query", []):
                    client.profile["playback"]["query"].pop(key, None)
                client.profile["playback"]["endpoint"] = variant.get("endpoint", baseline["endpoint"])
                client.profile["playback"]["request_mode"] = variant.get("request_mode", baseline["request_mode"])
                result.update(describe(client.raw_source(series_id, video_id, extra=variant["query"])))
            except Exception as error:
                result["error"] = safe_message(error)
            entry["variants"].append(result)
        # 页面只用于研究元数据布局；不保存带签名脚本或原始网页。
        page = client.transport.text(client.profile["catalog"]["page_template"].format(series_id=series_id, video_id=video_id))
        entry["page_structure"] = {"bytes": len(page), "markers": {name: page.count(name) for name in
                                   ("__PINIA__", "__pinia", "COVER_INFO", "VIDEO_INFO", "episodeMain", "video_ids", "__INITIAL_STATE__", "__NEXT_DATA__", "__NUXT__", "cid")},
                                   "assignments": sorted(set(re.findall(r'(?:window\.|var\s+|let\s+|const\s+)([\w$]+)\s*=', page))),
                                   "script_hosts": sorted({urlsplit(url).netloc for url in re.findall(r'<script[^>]+src="([^"]+)"', page)})}
        report["items"].append(entry)
    target = resolve_path(research["report_dir"], path.parent)
    target.mkdir(parents=True, exist_ok=True)
    output = target / f"playback_{datetime.now():%Y%m%d_%H%M%S}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"items": [{"note": item["note"], "page": item["page_structure"],
                                "variants": [{"name": value["name"], "error": value.get("error"),
                                              "formats": [(fmt["name"], fmt["width"], fmt["height"], fmt["vfps"]) for fmt in value.get("formats", [])],
                                              "audio_fields": value.get("audio_fields"), "video": value.get("video")}
                                             for value in item["variants"]]} for item in report["items"]]}, ensure_ascii=True))
    print(f"研究报告：{output}")


if __name__ == "__main__":
    main()
