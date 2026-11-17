"""提供带内容指纹的文件读取、原子记录写入与不覆盖发布接口。"""

from datetime import datetime
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from threading import Event

from .runtime import check_cancel


class OutputLocked(RuntimeError):
    """表示指定目录的跨进程锁正在被其他进程持有。"""


def validate_lock_file(value: str) -> None:
    """校验锁文件名 value 为无空白的单层 .lock 名称；非法时抛出 ValueError，无返回值。"""
    if (not isinstance(value, str) or not value.endswith(".lock") or value == ".lock"
            or re.search(r'[\s<>:"/\\|?*\x00-\x1f]', value)
            or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value)):
        raise ValueError("logging.lock_file 必须是无空白的单层 .lock 文件名")


@contextmanager
def output_lock(root: Path, lock_file: str):
    """在目录 root 持有 lock_file 指定的非阻塞平台锁；名称由调用方提供，冲突抛错，退出或崩溃释放；产生 None。"""
    validate_lock_file(lock_file)
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / lock_file
    if not path.resolve().is_relative_to(root):
        raise ValueError("输出锁链接超出输出根目录")
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise OutputLocked(f"无法获取本平台目录锁，可能已有本平台批次运行：{path}") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def timestamp() -> str:
    """返回带本地时区的当前 ISO 8601 时间；无参数。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def time_tag() -> str:
    """返回本地文件名时间戳 yymmdd_hhmmss；无参数。"""
    return datetime.now().astimezone().strftime("%y%m%d_%H%M%S")


def valid_digest(value) -> bool:
    """判断 value 是否为小写 SHA-256 十六进制字符串；返回布尔值。"""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def identity_digest(value) -> str:
    """对可序列化的稳定来源 value 计算规范 JSON SHA256；返回摘要，不接受 NaN。"""
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict:
    """读取 UTF-8 对象记录 path；返回字典，内容损坏或根节点非对象时抛错且不修改文件。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON 根节点必须为对象：{path}")
    return data


def atomic_json(path: Path, data: dict) -> None:
    """在 path 同目录独占创建临时文件并原子替换；data 为可序列化字典，失败清理临时文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fingerprint(path: Path, buffer_bytes: int, cached: dict | None = None, *, force: bool = False,
                cancel_event: Event | None = None) -> dict:
    """校验非空普通文件 path；buffer_bytes 为正块长，cached 为旧指纹，force 强制重算；返回大小、纳秒时间和摘要。

    cancel_event 为可选取消信号；大小与修改时间均未变的外部改写不在缓存检查范围，读取期间变化时拒绝保存摘要。
    """
    check_cancel(cancel_event)
    before = path.stat()
    if not path.is_file() or before.st_size <= 0:
        raise ValueError(f"内容指纹要求非空普通文件：{path}")
    stamp = {"size_bytes": before.st_size, "mtime_ns": before.st_mtime_ns}
    if (not force and isinstance(cached, dict) and valid_digest(cached.get("sha256"))
            and all(cached.get(key) == value for key, value in stamp.items())):
        return {**stamp, "sha256": cached["sha256"]}
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(buffer_bytes):
            check_cancel(cancel_event)
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"计算指纹时文件发生变化：{path}")
    return {**stamp, "sha256": digest.hexdigest()}


def publish(source: Path, target: Path, digest: str, buffer_bytes: int, *, cancel_event: Event | None = None,
            before_publish=None) -> dict:
    """将 source 校验复制到 target 的同盘临时文件后不覆盖发布；digest 为预期 SHA-256，返回成品指纹。

    cancel_event 为可选取消信号，before_publish 为重命名前的无参校验回调；两者失败均不发布。
    Windows 原子重命名拒绝已有目标，其他系统用同盘硬链接。
    """
    check_cancel(cancel_event)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".video-", suffix=".partial", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as dst, source.open("rb") as src:
            while chunk := src.read(buffer_bytes):
                check_cancel(cancel_event)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        result = fingerprint(temporary, buffer_bytes, cancel_event=cancel_event)
        if result["sha256"] != digest:
            raise RuntimeError(f"复制校验失败：{target}")
        check_cancel(cancel_event)
        if before_publish:
            before_publish()
        check_cancel(cancel_event)
        if os.name == "nt":
            temporary.rename(target)
        else:
            os.link(temporary, target)
        return result
    finally:
        temporary.unlink(missing_ok=True)
