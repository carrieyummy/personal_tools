"""隔离调用优酷请求签名及接口元数据解包，不触碰运行中客户端或导出媒体 DRM 密钥。"""

import json
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


def sample_vid(config: dict[str, Any]) -> str:
    """取得清单样本视频 ID；config 为已加载研究配置；返回 URL 中 vid，缺失时抛 ValueError。"""
    query = parse_qs(urlsplit(config["sample"]["url"]).query)
    values = query.get("vid", [])
    if len(values) != 1 or not values[0]:
        raise ValueError("当前研究样本 URL 必须带唯一非空 vid 参数")
    return values[0]


def run_native(config: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """执行隔离桥接；config 提供原生运行配置，data 为内存协议；返回结构化内存结果，调用方须脱敏。"""
    options = config["native"]
    folder = config["client_dir"]
    runner = Path(__file__).with_name("native_bridge.cjs")
    data = dict(data, module_path=str(folder / options["module"]))
    env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
    try:
        run = subprocess.run([str(folder / options["executable"]), str(runner)], cwd=folder, env=env,
                             input=json.dumps(data), capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=options["timeout_seconds"], check=False)
    except subprocess.TimeoutExpired:
        return {"error": "TimeoutExpired", "timeout_seconds": options["timeout_seconds"]}
    # 原生 stdout/stderr 可能包含敏感调试信息，始终丢弃；仅解析约定结果行。
    lines = [line for line in run.stdout.splitlines() if line.startswith("YOUKU_RESEARCH_RESULT=")]
    if not lines:
        return {"error": "NoStructuredResult", "returncode": run.returncode}
    result = json.loads(lines[-1].split("=", 1)[1])
    result["returncode"] = run.returncode
    result["module_sha256"] = hashlib.sha256((folder / options["module"]).read_bytes()).hexdigest()
    return result


def native_signature(config: dict[str, Any]) -> dict[str, Any]:
    """生成请求签名及挑战；config 为研究配置；返回仅供内存请求使用的结果，禁止原样保存。"""
    options = config["native"]
    return run_native(config, {"operation": "sign", "vid": sample_vid(config),
                               "ccode": options["ccode"], "client_ip": options["client_ip"]})


def native_decode_metadata(config: dict[str, Any], encoded: str) -> dict[str, Any]:
    """解包 UPS 元数据；config 为运行配置，encoded 为接口密文；返回内存内容。当前样本未触发，真实分支待验证。"""
    return run_native(config, {"operation": "decode_metadata", "encoded_metadata": encoded})


def signature_summary(result: dict[str, Any]) -> dict[str, Any]:
    """脱敏原生调用结果；result 可含内存签名；返回运行时、退出码和签名长度，绝不返回签名本身。"""
    return {"runtime": result.get("runtime"), "returncode": result.get("returncode"),
            "error": result.get("error"), "signature_present": bool(result.get("ckey")),
            "signature_length": len(result.get("ckey") or ""), "module_sha256": result.get("module_sha256")}
