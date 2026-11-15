"""文件指纹、跨进程锁、断点记录与无覆盖发布；共享目录仅维护带 mgtv 前缀的索引。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from threading import Event, Lock
import time
from typing import Any
from uuid import uuid4

from .client import check_cancel
from .errors import ToolError


def digest(value: Any) -> str:
    """计算结构指纹；value 为 JSON 可编码的稳定身份；返回 SHA256 十六进制串。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_hash(path: Path, buffer: int, cancel: Event | None = None) -> str:
    """流式完整指纹；path 为文件，buffer 为每次字节数，cancel 可中止；返回 SHA256，不使用时间戳缓存。"""
    result = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(buffer):
            check_cancel(cancel)
            result.update(block)
    return result.hexdigest()


def read_json(path: Path, initial: Any) -> Any:
    """读取记录；path 不存在时返回 initial，损坏时停止，避免覆盖旧记录；返回 JSON 值。"""
    if not path.exists():
        return initial
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ToolError(f"记录无法读取，请检查：{path}") from error


def atomic_json(path: Path, value: Any, settings: dict[str, Any]) -> None:
    """原子写记录；path 为目标，value 为 JSON 值，settings 控制替换重试；无返回值，旧文件不提前删除。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        for attempt in range(settings["write_retries"] + 1):
            try:
                temporary.replace(path)
                return
            except OSError:
                if attempt == settings["write_retries"]:
                    raise
                time.sleep(settings["write_retry_interval_seconds"])
    finally:
        temporary.unlink(missing_ok=True)


def safe_name(value: str, maximum: int) -> str:
    """生成 Windows 安全路径片段；value 为源站标题，maximum 为长度上限；返回不含空白/分隔符的名称。"""
    result = re.sub(r'[<>:"/\\|?*\x00-\x20\s]', "_", str(value)).strip(". ")[:maximum].rstrip(".")
    if not result:
        raise ToolError("源站标题不能生成有效文件名")
    if result.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        result = "_" + result
    return result


class DirectoryLock(AbstractContextManager):
    """进程生命周期内锁定根目录；path 为专用锁文件，不删除锁文件以避免解除后重建竞争。"""

    def __init__(self, path: Path):
        """保存 path；无返回值，不立即加锁。"""
        self.path, self.handle = path, None

    def __enter__(self):
        """非阻塞获取独占字节锁；无参数；返回自身，冲突时报错，崩溃后系统自动释放。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise ToolError(f"目录正在被其他芒果任务使用：{self.path.parent}") from None
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """关闭文件并释放锁；参数为上下文异常信息；返回 False 以保留异常。"""
        if self.handle:
            self.handle.close()
        return False


class Publisher:
    """单一提交器；config 定义输出和命名，cancel 可取消复制；索引仅记录验证完毕的成品。"""

    def __init__(self, config: dict[str, Any], cancel: Event):
        """保存 config/cancel 并读取索引；损坏索引直接报错；无返回值。"""
        self.config, self.cancel = config, cancel
        self.root, self.settings = config["output_path"].resolve(), config["logging"]
        self.path = self.root / self.settings["index_file"]
        self.index = read_json(self.path, {"schema": 1, "videos": {}})
        if self.index.get("schema") != 1 or not isinstance(self.index.get("videos"), dict):
            raise ToolError("成品索引格式无效")
        self.lock = Lock()

    def reusable(self, key: str, identity: str) -> dict[str, Any] | None:
        """检查成品复用；key 为视频编号，identity 为源指纹；返回有完整解码证据且成品指纹吻合的记录，否则 None。"""
        with self.lock:
            versions = list(self.index["videos"].get(key, []))
        for record in reversed(versions):
            if record["identity"] != identity:
                continue
            verification = record.get("verification", {})
            if verification.get("exit_code") != 0 or verification.get("decoded_frames", 0) <= 0 or verification.get("decoded_seconds", 0) <= 0:
                continue
            path = (self.root / record["path"]).resolve()
            if not path.is_relative_to(self.root):
                raise ToolError("索引成品路径越出输出目录")
            if path.is_file() and path.stat().st_size == record["bytes"] and file_hash(path, self.config["download"]["buffer_bytes"], self.cancel) == record["sha256"]:
                return record
        return None

    def publish(self, source: Path, relative: Path, key: str, identity: str, evidence: dict[str, Any]) -> dict[str, Any]:
        """复制并提交成品；source 为已验证 MP4，relative 为目标路径，key/identity 为身份，evidence 为验证记录；返回索引项。"""
        with self.lock:
            target = (self.root / relative).resolve()
            if not target.is_relative_to(self.root):
                raise ToolError("成品路径越出输出目录")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                stamp = datetime.now().strftime(self.config["naming"]["timestamp_format"])
                base, sequence = target, 0
                while target.exists():
                    suffix = f"_{stamp}" + (f"_{sequence}" if sequence else "")
                    target = base.with_stem(base.stem + suffix)
                    sequence += 1
            temporary = target.with_name(f".mgtv_{uuid4().hex}.tmp")
            previous = list(self.index["videos"].get(key, []))
            try:
                with source.open("rb") as input_file, temporary.open("xb") as output:
                    while block := input_file.read(self.config["download"]["buffer_bytes"]):
                        check_cancel(self.cancel)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                expected = file_hash(source, self.config["download"]["buffer_bytes"], self.cancel)
                if file_hash(temporary, self.config["download"]["buffer_bytes"], self.cancel) != expected:
                    raise ToolError("目标盘复制指纹不一致")
                check_cancel(self.cancel)
                if os.name == "nt":
                    os.rename(temporary, target)  # Windows rename 不覆盖已存在文件。
                else:
                    os.link(temporary, target)
                    temporary.unlink()
                record = {"identity": identity, "path": target.relative_to(self.root).as_posix(), "sha256": expected,
                          "bytes": target.stat().st_size, "published_at": datetime.now().isoformat(timespec="seconds"), **evidence}
                self.index["videos"][key] = [*previous, record]
                try:
                    atomic_json(self.path, self.index, self.settings)
                except BaseException:
                    self.index["videos"][key] = previous
                    target.unlink()
                    raise
                return record
            finally:
                temporary.unlink(missing_ok=True)
