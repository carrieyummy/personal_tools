"""绑定当前播放会话与已校验样本，隔离复用原生处理函数，再按 PES 负载无重编码还原。"""

import base64
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.live import LiveInspector, evaluate_player
from lib.native import native_challenge, sample_vid
from lib.transport import decrypt_transport


def current_material(config: dict[str, Any], pid: int) -> tuple[dict, dict]:
    """读取当前精确样本的原生授权材料；config 为配置，pid 为主进程；返回内存材料及不含凭据的调试生命周期信息。"""
    args = {"vid": sample_vid(config), "type": config["media"]["stream_type"],
            "fields": config["cache"]["stream_fields"], "key_index": config["restoration"]["key_index"]}
    expression = "((o)=>{const c=window.videoPlayer.context,r=c.originResource;if(c.config.vid!==o.vid||r.video.encodeid!==o.vid)throw Error('SampleMismatch');"
    expression += "if(c.drm.keyIndex!==o.key_index)throw Error('KeyIndexMismatch');const s=r.stream.filter(s=>s.stream_type===o.type);if(s.length!==1)throw Error('StreamNotUnique');"
    expression += "return {client:c.drm.encryptR1,server:s[0].encryptR_server,copyright:s[0].stream_ext.copyright_key,stream:Object.fromEntries(o.fields.filter(k=>k in s[0]).map(k=>[k,s[0][k]]))};})(" + json.dumps(args) + ")"
    with LiveInspector(config, pid) as inspector:
        material = evaluate_player(config, inspector, expression)
    return material, {"activated_by_probe": inspector.activated, "owned_inspector_closed": inspector.closed_owned_inspector}


from local.youku.authorization import session_key


def restore_samples(config: dict[str, Any], pid: int, input_path: Path) -> dict[str, Any]:
    """还原已下载并校验的帧享抽样；config 为配置，pid 为客户端进程，input_path 为 media 报告；返回可交给 media-verify 的候选报告。"""
    report = json.loads(input_path.read_text(encoding="utf-8"))
    if report["sample"] != config["sample"] or report["stream"]["stream_type"] != config["media"]["stream_type"]:
        raise ValueError("RestorationSampleMismatch")
    material, lifecycle = current_material(config, pid)
    for field in ("stream_type", "width", "height", "size", "audio_lang", "drm_type"):
        if report["stream"].get(field) != material["stream"].get(field):
            raise ValueError("RestorationStreamMismatch")
    root = config["media"]["work_dir"].resolve()
    folder = (root / report["work_subdir"]).resolve()
    if not folder.is_relative_to(root):
        raise ValueError("RestorationPathRejected")
    sources = []
    if not report["samples"] or len(report["samples"]) > len(config["media"]["sample_positions"]):
        raise ValueError("RestorationSampleCountInvalid")
    for sample in report["samples"]:
        path = (folder / sample["file"]).resolve()
        if not path.is_relative_to(folder) or path.suffix != ".ts":
            raise ValueError("RestorationPathRejected")
        if path.stat().st_size > config["media"]["max_segment_bytes"]:
            raise ValueError("RestorationSampleTooLarge")
        data = path.read_bytes()
        if len(data) != sample["bytes"] or hashlib.sha256(data).hexdigest() != sample["sha256"]:
            raise ValueError("RestorationHashMismatch")
        sources.append((sample, data))
    key = session_key(config, material)
    result = {"observed_at_utc": datetime.now(timezone.utc).isoformat(), "sample": report["sample"],
              "stream": report["stream"], "work_subdir": report["work_subdir"], "samples": [],
              "inspector": lifecycle, "adapter": {name: config["restoration"][name] for name in
                  ("library", "library_sha256", "challenge_module_sha256", "function_rva")}, "independent_file_verified": False}
    for sample, data in sources:
        if sample.get("private", {}).get("DRM_NOTENCRYPT") == 1:
            raise ValueError("RestorationClearSegmentNotSupported")
        restored, stats = decrypt_transport(data, key)
        digest = hashlib.sha256(restored).hexdigest()
        filename = "restored_" + str(sample["index"]) + "_" + digest[:16] + ".ts"
        target = folder / filename
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError("RestorationExistingOutputMismatch")
        if not target.exists():
            with target.open("xb") as handle:
                handle.write(restored)
        result["samples"].append({"file": filename, "bytes": len(restored), "sha256": digest,
                                  "source_sha256": sample["sha256"], "index": sample["index"],
                                  "duration": sample["duration"], "pes": stats})
    return result
