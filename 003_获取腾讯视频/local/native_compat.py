"""协调客户端 DLL 指纹更新，保留用户 YAML 格式并防止并发覆盖。"""

from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
from threading import Event
from uuid import uuid4

import yaml

from .configuration import resolve_path
from .runtime import check_cancel
from .storage import OutputLocked, output_lock


@contextmanager
def profile_lock(profile_path: Path, cancel: Event, interval: float):
    """等待 profile_path 对应的独立跨进程锁；cancel 可取消，interval 为轮询秒数。"""
    lock_root = profile_path.parent / f".{profile_path.name}.lock"
    while True:
        check_cancel(cancel)
        stack = ExitStack()
        try:
            stack.enter_context(output_lock(lock_root, profile_path.name + ".lock"))
            break
        except OutputLocked:
            stack.close()
            cancel.wait(interval)
    with stack:
        check_cancel(cancel)
        yield


def read_profile(profile_path: Path) -> tuple[bytes, dict]:
    """读取 profile_path 的原始 UTF-8 字节和解析后的配置，DLL 根目录解析为绝对路径。"""
    raw = profile_path.read_bytes()
    profile = yaml.safe_load(raw.decode("utf-8"))
    profile["dll_root"] = resolve_path(profile["dll_root"], profile_path.parent)
    return raw, profile


def update_digest(profile_path: Path, original: bytes, old_digest: str, new_digest: str) -> None:
    """仅更新 profile_path 中的指纹标量；original 必须保持未变，保留 YAML 注释与其他内容。"""
    text = original.decode("utf-8")
    root = yaml.compose(text)
    matches = [value for key, value in root.value if key.value == "dll_sha256"]
    if len(matches) != 1 or matches[0].value != old_digest:
        raise ValueError("DLL 配置中的指纹字段不唯一或已改变")
    node = matches[0]
    start, end = node.start_mark.index, node.end_mark.index
    scalar = text[start:end]
    if scalar.count(old_digest) != 1:
        raise ValueError("DLL 指纹字段格式无法安全更新")
    updated = text[:start] + scalar.replace(old_digest, new_digest, 1) + text[end:]
    temporary = profile_path.with_name(f".{profile_path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if profile_path.read_bytes() != original:
            raise RuntimeError("DLL 配置在验收期间被修改，未更新指纹")
        temporary.replace(profile_path)
    finally:
        temporary.unlink(missing_ok=True)
