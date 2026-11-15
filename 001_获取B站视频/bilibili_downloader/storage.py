"""以可回滚事务协调媒体移动和多份 JSON 更新，统一限制下载目录边界。"""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import sys
import tempfile
from threading import RLock
from typing import Callable, Iterator

from .errors import ToolError


# Windows 的 os.replace 不接受目标仍有打开句柄，即使读者允许删除共享也如此。
# 仅串行化短暂的记录读盘/替换，不覆盖 JSON 解析、网络、媒体探测或 FFmpeg。
record_io_lock = RLock()

# 嵌套索引事务共用待清理集合；最外层成功后再删除空父目录，保留失败回滚所需的旧目录。
_pending_directory_cleanup: ContextVar[set[Path] | None] = ContextVar('pending_directory_cleanup', default=None)


def validate_path(path: Path, boundary: Path) -> None:
    """校验 path 位于绝对边界 boundary 内且祖先不含链接；非法时抛出 ToolError，无返回值。"""
    boundary = boundary.resolve()
    if path == boundary or not path.resolve().is_relative_to(boundary):
        raise ToolError(f'存储路径超出下载目录：{path}')
    current = path
    # Windows 临时目录可能使用 ADMINI~1 等短路径，不能依赖字符串祖先与已解析边界相等来结束循环。
    while current != boundary and current != current.parent:
        if current.is_symlink() or current.is_junction():
            raise ToolError(f'存储路径不能包含链接：{current}')
        current = current.parent


@contextmanager
def output_directory(folder: Path, boundary: Path) -> Iterator[None]:
    """在 boundary 内准备 folder，退出时清理本次新建的空目录；产生 None，不删除已有目录或结果。"""
    validate_path(folder, boundary)
    existed = folder.exists()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        if not existed:
            try:
                folder.rmdir()
            except OSError:
                pass


def apply_changes(boundary: Path, moves: list[tuple[Path, Path]], records: list[Path],
                  commit: Callable[[], None], deletes: list[Path] | None = None) -> None:
    """在 boundary 内移动 moves 并调用 commit 写 records，最后删除 deletes 中的旧记录。

    所有目标先检查冲突；异常时恢复媒体、记录原始字节及已创建的空目录，无返回值。
    删除记录遗留的空父目录在最外层事务成功后清理；保留非空目录、链接及下载根目录。
    commit 只应修改 records/deletes 列出的 JSON，不应再执行媒体移动。
    """
    boundary = boundary.resolve()
    deletes = deletes or []
    moves = [(old, new) for old, new in moves if old != new]
    targets: set[str] = set()
    for old, new in moves:
        validate_path(old, boundary)
        validate_path(new, boundary)
        key = new.as_posix().casefold()
        if key in targets or new.exists():
            raise ToolError(f'迁移存在同名目标，不覆盖：{new}')
        targets.add(key)
        if old.is_dir() and any(p.is_symlink() or p.is_junction() for p in old.rglob('*')):
            raise ToolError(f'迁移源包含链接：{old}')
    for path in [*records, *deletes]:
        validate_path(path, boundary)
        if path.exists() and not path.is_file():
            raise ToolError(f'记录路径不是文件：{path}')
    originals = {path: path.read_bytes() if path.exists() else None for path in [*records, *deletes]}
    moved: list[tuple[Path, Path]] = []
    created: list[Path] = []
    cleanup = _pending_directory_cleanup.get()
    token = None
    if cleanup is None:
        cleanup = set()
        token = _pending_directory_cleanup.set(cleanup)
    try:
        for destination in [*(new for _, new in moves), *records]:
            missing, parent = [], destination.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                created.append(directory)
        for old, new in moves:
            old.rename(new)
            moved.append((old, new))
        commit()
        for path in deletes:
            if path not in records:
                path.unlink(missing_ok=True)
                if originals[path] is not None and path.parent.resolve() != boundary:
                    cleanup.add(path.parent)
    except BaseException as exc:
        for path, content in originals.items():
            try:
                if content is None:
                    path.unlink(missing_ok=True)
                elif not path.exists() or path.read_bytes() != content:
                    path.write_bytes(content)
            except OSError as error:
                exc.add_note(f'记录回滚失败：{path}：{error}')
        for old, new in reversed(moved):
            try:
                new.rename(old)
            except OSError as error:
                exc.add_note(f'文件回滚失败：{new} -> {old}：{error}')
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    finally:
        if token is not None:
            _pending_directory_cleanup.reset(token)
    if token is not None:
        for directory in sorted(cleanup, key=lambda path: len(path.parts), reverse=True):
            try:
                if directory.is_symlink() or directory.is_junction():
                    continue
                directory.rmdir()
            except OSError:
                # 仅删除已经变空的旧记录目录；其他任务的索引、媒体和断点继续保留。
                pass


def replace_file_transaction(boundary: Path, source: Path, destination: Path,
                             records: list[Path], commit: Callable[[], None]) -> bool:
    """把完整 source 保存到 boundary 内的 destination，并调用 commit 更新 records；返回是否覆盖旧文件。

    旧文件暂存于目标目录，成功后删除；移动、索引写入或取消失败时恢复旧文件及原索引。
    回滚失败时保留旧文件备份并在异常附注中指出位置，不删除可恢复的旧成品。
    """
    validate_path(source, boundary)
    validate_path(destination, boundary)
    if not source.is_file() or not source.stat().st_size:
        raise ToolError(f'待保存文件不存在或为空：{source}')
    if destination.exists() and not destination.is_file():
        raise ToolError(f'合并目标不是普通文件：{destination}')
    backup = None
    if destination.exists():
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.merge-backup-', suffix='.bak', delete=False) as stream:
            backup = Path(stream.name)
        try:
            destination.replace(backup)
        except BaseException as exc:
            try:
                backup.unlink(missing_ok=True)
            except OSError as error:
                exc.add_note(f'空备份清理失败：{backup}：{error}')
            raise
    try:
        apply_changes(boundary, [(source, destination)], records, commit)
    except BaseException as exc:
        if backup is not None:
            try:
                backup.replace(destination)
            except OSError as error:
                exc.add_note(f'旧合并文件恢复失败：{error}；原文件仍保留在：{backup}')
        raise
    if backup is not None:
        try:
            backup.unlink()
        except OSError as error:
            print(f'合并已成功，旧文件备份未能清理：{backup}：{error}', file=sys.stderr)
    return backup is not None
