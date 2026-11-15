"""供隔离子进程调用的 DLL 真实样本还原，不写入用户成品或修改配置。"""

import json
from pathlib import Path
import sys

from .native import latest_dll
from .storage import atomic_json
from .workflow import extract


def probe(request_path: Path) -> None:
    """读取 request_path 的候选配置和分片，写入独立 TS 与来源摘要；不修改源文件。"""
    request = json.loads(request_path.read_text(encoding="utf-8"))
    config = request["config"]
    config["native"]["dll_root"] = Path(config["native"]["dll_root"])
    expected = Path(request["dll_path"])
    if latest_dll(config["native"]["dll_root"], config["native"]["dll_name"]) != expected:
        raise RuntimeError("DLL 验收开始前客户端版本已变化")
    segments = request["segments"]
    for segment in segments:
        segment["path"] = Path(segment["path"])
    sources = extract(config, segments, Path(request["folder"]), Path(request["target"]))
    atomic_json(Path(request["result"]), {"sources": sources})


def main() -> None:
    """按内部调用协议读取唯一的请求文件参数；失败由子进程返回非零退出码。"""
    if len(sys.argv) != 2:
        raise ValueError("DLL 隔离验收需要一个请求文件")
    probe(Path(sys.argv[1]))
