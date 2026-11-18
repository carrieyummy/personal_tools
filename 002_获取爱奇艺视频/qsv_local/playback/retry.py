"""将单集重新下载限制在其已登记的工作目录内。"""

from pathlib import Path
import shutil
from stat import FILE_ATTRIBUTE_REPARSE_POINT, S_ISDIR, S_ISREG

from ..storage import read_json, valid_digest


def clear_episode_work(root: Path, output_root: Path, tvid: str, key: str) -> None:
    """清理 root 下属于 tvid 的已登记来源目录 key，缺失时直接返回。

    output_root 是成品根目录。路径或登记身份不一致时抛错，不接管其他目录。调用方持有工作根锁和来源锁。
    """
    owner, _, digest = key.partition("_")
    if owner != tvid or not owner.isascii() or not owner.isdecimal() or not valid_digest(digest):
        raise ValueError("重试工作目录的来源标识无效")
    root = root.resolve()
    work = root / key
    try:
        info = work.lstat()
    except FileNotFoundError:
        return
    if (not S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
            or work.resolve() != work or output_root.resolve().is_relative_to(work)):
        raise ValueError("重试工作目录不是可清理的普通目录")
    record = work / "source_identity.json"
    if not record.exists() and not any(work.iterdir()):
        work.rmdir()
        return
    info = record.lstat()
    if not S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("重试工作目录的来源记录无效")
    saved = read_json(record)
    if saved.get("tvid") != tvid or saved.get("sha256") != digest:
        raise ValueError("重试工作目录与登记来源不一致")
    shutil.rmtree(work)
