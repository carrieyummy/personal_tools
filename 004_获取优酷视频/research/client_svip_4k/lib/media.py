"""采样当前帧享媒体并检查本地可解码性；不把保护流当作可直接合并的明文，也不输出密钥。"""

from __future__ import annotations

from local.youku.manifest import MediaRedirect, fetch_media, parse_manifest, require_media_url, transport_summary
import hashlib
import json
import math
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from lib.live import LiveInspector, evaluate_player
from lib.native import sample_vid












def sample_media(config: dict[str, Any], pid: int) -> dict[str, Any]:
    """采集当前样本的配置档位和前中后媒体；config 为配置，pid 为优酷主进程；返回脱敏报告，样本写忽略目录。"""
    options = config["media"]
    args = {"vid": sample_vid(config), "stream_type": options["stream_type"], "fields": config["cache"]["stream_fields"]}
    expression = "((o)=>{const c=window.videoPlayer.context,r=c.originResource;if(c.config.vid!==o.vid||r.video.encodeid!==o.vid)throw Error('SampleMismatch');"
    expression += "const s=r.stream.filter(s=>s.stream_type===o.stream_type);if(s.length!==1)throw Error('StreamNotUnique');"
    expression += "return {url:s[0].m3u8_url,stream:Object.fromEntries(o.fields.filter(k=>k in s[0]).map(k=>[k,s[0][k]]))};})(" + json.dumps(args) + ")"
    with LiveInspector(config, pid) as inspector:
        source = evaluate_player(config, inspector, expression)
    raw, final_url, fetched = fetch_media(options, source["url"], options["max_manifest_bytes"])
    manifest = parse_manifest(raw, final_url)
    segments = manifest.pop("segments")
    source_id = hashlib.sha256((sample_vid(config) + "\0" + options["stream_type"]).encode()).hexdigest()[:20]
    folder = options["work_dir"] / source_id
    folder.mkdir(parents=True, exist_ok=True)
    report = {"observed_at_utc": datetime.now(timezone.utc).isoformat(), "sample": config["sample"],
              "stream": source["stream"], "work_subdir": source_id, "manifest": dict(manifest, fetch=fetched,
              segment_count=len(segments)), "samples": [], "independent_file_verified": False,
              "inspector": {"activated_by_probe": inspector.activated, "owned_inspector_closed": inspector.closed_owned_inspector}}
    indices = sorted({round(position * (len(segments) - 1)) for position in options["sample_positions"]})
    for index in indices:
        segment = segments[index]
        data, _, fetched = fetch_media(options, segment["url"], options["max_segment_bytes"])
        declared = segment["private"].get("FILESIZE")
        if declared is not None and len(data) != declared:
            raise ValueError("MediaSegmentDeclaredSizeMismatch")
        digest = hashlib.sha256(data).hexdigest()
        filename = f"segment_{index:04d}_{digest[:16]}.ts"
        path = folder / filename
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("MediaExistingSampleMismatch")
        if not path.exists():
            with path.open("xb") as handle:
                handle.write(data)
        report["samples"].append(dict(fetched, index=index, file=filename, duration=segment["duration"],
                                     private=segment["private"], discontinuity=segment["discontinuity"],
                                     sha256=digest, transport=transport_summary(data)))
    return report


def verify_media(config: dict[str, Any], input_path: Path) -> dict[str, Any]:
    """完整解码本轮抽样文件；config 为配置，input_path 为脱敏采样报告；返回摘要，无论抽样成功与否都不宣称全片完成。"""
    report = json.loads(input_path.read_text(encoding="utf-8"))
    options = config["media"]
    if report.get("sample") != config["sample"] or report.get("stream", {}).get("stream_type") != options["stream_type"]:
        raise ValueError("MediaVerificationSourceMismatch")
    root = options["work_dir"].resolve()
    folder = (root / report["work_subdir"]).resolve()
    if not folder.is_relative_to(root):
        raise ValueError("MediaVerificationPathRejected")
    result = {"observed_at_utc": datetime.now(timezone.utc).isoformat(), "sample": report["sample"],
              "stream": report["stream"], "samples": [], "independent_file_verified": False}
    for sample in report["samples"]:
        path = (folder / sample["file"]).resolve()
        if not path.is_relative_to(folder) or path.suffix != ".ts":
            raise ValueError("MediaVerificationPathRejected")
        if path.stat().st_size != sample["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != sample["sha256"]:
            raise ValueError("MediaVerificationHashMismatch")
        command = [str(options["ffmpeg"]), "-hide_banner", "-v", "warning", "-xerror",
                   "-protocol_whitelist", "file", "-f", "mpegts", "-i", str(path),
                   "-map", "0:v:0", "-map", "0:a:0", "-f", "framehash", "-hash", "sha256", "-"]
        try:
            run = subprocess.run(command, capture_output=True, timeout=options["decode_timeout_seconds"], check=False)
            stderr = run.stderr.decode("utf-8", errors="replace")
            counts = Counter()
            for line in run.stdout.decode("utf-8", errors="replace").splitlines():
                fields = [field.strip() for field in line.split(",")]
                if len(fields) == 6 and fields[0] in ("0", "1") and fields[4].isdigit() and int(fields[4]) > 0:
                    counts[fields[0]] += 1
            item = {"file": sample["file"], "sha256": sample["sha256"], "exit_code": run.returncode,
                    "decode_passed": run.returncode == 0 and counts["0"] > 0 and counts["1"] > 0,
                    "decoded_video_frames": counts["0"], "decoded_audio_frames": counts["1"], "timed_out": False,
                    "diagnostics": {name: phrase in stderr for name, phrase in {
                        "invalid_data": "Invalid data", "missing_video_format": "Cannot determine format",
                        "missing_codec_parameters": "Could not find codec parameters",
                        "empty_output": "nothing was written"}.items()}}
        except subprocess.TimeoutExpired:
            item = {"file": sample["file"], "sha256": sample["sha256"], "exit_code": None,
                    "decode_passed": False, "timed_out": True}
        result["samples"].append(item)
    result["all_samples_decoded"] = bool(result["samples"]) and all(item["decode_passed"] for item in result["samples"])
    return result
