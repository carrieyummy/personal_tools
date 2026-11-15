"""通过隔离子进程读取客户端当前登录态；不采用历史 Cookie 或播放日志中的票据。"""

import json
import os
from pathlib import Path
import subprocess
import sys

from .errors import AuthenticationError


def load_user(config: dict) -> dict:
    """按 config 在隔离进程读取原生用户对象；返回 tokens/app_version，异常不包含票据或 DLL 原始日志。"""
    command = [sys.executable, "-m", "local.playback.native_user", "--profile", str(config["playback_profile"])]
    try:
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[2],
                                capture_output=True, timeout=config["network"]["timeout_seconds"],
                                env={**os.environ, "PYTHONUTF8": "1"},
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise AuthenticationError("读取腾讯客户端登录态失败或超时") from None
    if result.returncode:
        raise AuthenticationError("客户端只读登录接口不可用；请检查登录 DLL 版本及专项配置")
    try:
        value = json.loads(result.stdout)
        if not value["tokens"]["vuserid"] or not value["tokens"]["vusession"]:
            raise ValueError("empty session")
        return value
    except (ValueError, KeyError, TypeError):
        raise AuthenticationError("客户端没有有效登录态，请先在腾讯客户端登录") from None
