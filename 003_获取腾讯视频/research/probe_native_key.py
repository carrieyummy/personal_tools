"""核对隔离原生签名与同档播放请求；不保存签名、票据或媒体地址，不更改正式下载协议。"""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.configuration import resolve_path
from local.playback.client import NativeClient, video_info
from research.settings import sample_requests as requests
from local.playback.transport import safe_message
from local.storage import atomic_json
from research.observe_native_playback import probe_media
from research.settings import load_research
from research.playback_probe import describe


def native_key(path: Path, settings: dict, request: dict, timeout: float, *, pid: int | None = None) -> str:
    """按 path/settings 在隔离进程或显式 pid 上下文生成 request 的签名；timeout 限制隔离调用；返回内存字符串。"""
    if pid is not None:
        from research.native_key_context import generate
        _, config = load_research(path)
        key = generate(config, pid, request)
    else:
        command = [sys.executable, "-X", "utf8", str(Path(__file__).with_name("native_key_worker.py")), "--config", str(path)]
        result = subprocess.run(command, input=json.dumps(request).encode(), capture_output=True,
                                cwd=path.parent.parent, timeout=timeout,
                                env={**os.environ, "PYTHONUTF8": "1"}, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError(f"隔离签名调用失败，进程退出码 {result.returncode}")
        try:
            key = json.loads(result.stdout)["ckey"]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("隔离签名未返回已核对的内存结构") from None
    if not isinstance(key, str) or not 0 < len(key) <= settings["max_output_bytes"]:
        raise RuntimeError("隔离签名结果类型或长度非法")
    if re.fullmatch(r"-?\d{1,5}", key):
        raise RuntimeError(f"原生签名接口返回错误码 {key}")
    return key


def main() -> int:
    """读取 --config 的 ABI、清单及边界；--local-only 仅核对签名结构，--pid 指定运行上下文；保存脱敏报告并返回退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--local-only", action="store_true", help="可选：仅核对签名结构；同时省略 --pid 时独立加载 DLL")
    parser.add_argument("--pid", type=int, help="可选：指定已核对播放器；播放查询省略时按正式配置自动匹配")
    args = parser.parse_args()
    path = args.config.resolve()
    research, config = load_research(path)
    settings = research["native_observation"]["signing"]
    entries = []
    for item in requests(config):
        entry = {"note": item.note, "video_id": item.video_id, "local_only": args.local_only}
        try:
            if args.local_only:
                key = native_key(path, settings, {"vid": item.video_id, "guid": "{" + str(uuid4()).upper() + "}",
                                                 "timestamp": int(time.time())}, config["network"]["timeout_seconds"], pid=args.pid)
                entry.update(signature_length=len(key), base64_alphabet_only=bool(re.fullmatch(r"[A-Za-z0-9+/=_-]+", key)))
            else:
                trial = deepcopy(config)
                profile = trial["playback"]["playback"]
                variant = next(value for value in research["transfer_probe"]["delivery_variants"]
                               if value["name"] == research["native_key_probe"]["delivery_variant"])
                profile.update(endpoint=variant["endpoint"], request_mode=variant["request_mode"], app_version=settings["app_version"])
                profile["query"].update(variant["query"], platform=str(settings["platform"]),
                                        encryptVer=research["native_key_probe"]["encrypt_version"])
                client = NativeClient(trial)
                client.signer_pid = args.pid
                data = client.raw_source(item.series_id, item.video_id, research["native_key_probe"]["definition"])
                entry.update(code=data.get("code"), em=data.get("em"), exem=data.get("exem"), login=data.get("login"))
                if str(data.get("code")) not in profile["success_codes"] or data.get("login") not in profile["authenticated_states"]:
                    entry["error"] = "播放接口未接受当前签名或登录请求"
                if data.get("vl"):
                    audio_description = describe(data)
                    entry["audio_schema"] = audio_description["audio_schema"]
                    entry["audio_bindings"] = audio_description["audio_bindings"]
                    video = video_info(data)
                    entry["video"] = {name: video.get(name) for name in ("vw", "vh", "fps", "st", "td")}
                    entry["probes"] = [probe_media(client.transport, value["url"] + value.get("hls", {}).get("pt", ""),
                                                   research["transfer_probe"]) for value in video["ul"]["ui"]
                                       if value["vt"] not in research["transfer_probe"]["excluded_delivery_types"]]
        except Exception as error:
            entry["error"] = safe_message(error)
        entries.append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
    target = resolve_path(research["report_dir"], path.parent) / "native_key_probe.json"
    atomic_json(target, {"entries": entries})
    print(f"原生签名隔离研究报告：{target}")
    return int(any(entry.get("error") for entry in entries))


if __name__ == "__main__":
    raise SystemExit(main())
