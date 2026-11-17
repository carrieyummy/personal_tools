"""通过已校验原生模块在隔离进程中还原本次会话材料；不持久化密钥。"""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
from ..errors import ProtocolError
from .native import native_challenge

def session_key(config: dict[str, Any], material: dict) -> bytes:
    """隔离处理当前会话授权材料；config 为配置，material 含同一响应的三元组；返回内存密钥，子进程原始日志始终丢弃。"""
    options = config["restoration"]
    library = config["client_dir"] / options["library"]
    if hashlib.sha256(library.read_bytes()).hexdigest() != options["library_sha256"]:
        raise ProtocolError("NativeLibraryVersionMismatch")
    module = config["client_dir"] / config["native"]["module"]
    if hashlib.sha256(module.read_bytes()).hexdigest() != options["challenge_module_sha256"]:
        raise ProtocolError("NativeChallengeVersionMismatch")
    request = {"library": str(library), "sha256": options["library_sha256"],
               "function_rva": options["function_rva"], "output_bytes": options["output_bytes"],
               "material": [native_challenge(config, material["client"]), material["server"], material["copyright"]]}
    try:
        run = subprocess.run([sys.executable, str(Path(__file__).with_name("key_bridge.py"))],
                             input=json.dumps(request), text=True, encoding="utf-8", errors="replace", capture_output=True,
                             timeout=options["timeout_seconds"], check=False)
    except subprocess.TimeoutExpired:
        raise ProtocolError("NativeMediaTimeout") from None
    lines = [line for line in run.stdout.splitlines() if line.startswith("YOUKU_KEY_RESULT=")]
    if run.returncode != 0 or len(lines) != 1:
        raise ProtocolError("NativeMediaWorkerFailed")
    result = json.loads(lines[0].split("=", 1)[1])
    key = base64.b64decode(result.get("key", ""), validate=True)
    if len(key) != 16:
        raise ProtocolError("NativeMediaResultInvalid")
    return key
