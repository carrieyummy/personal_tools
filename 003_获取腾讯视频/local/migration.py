"""在输出锁内为既有腾讯剧目录补首播日期，保留索引并同步运行历史。"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from string import Formatter

from .configuration import series_directory_name
from .premiere import album_date
from .storage import atomic_json, output_lock


def snapshot(folder: Path) -> list[tuple]:
    """读取 folder 内全部普通文件的相对路径、大小及修改时间；拒绝越界链接，返回可比较快照。"""
    result = []
    pending = [folder]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                    raise ValueError("剧集目录包含链接或重解析点，停止迁移")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    result.append((path.relative_to(folder).as_posix(), info.st_size, info.st_mtime_ns))
                else:
                    raise ValueError("剧集目录包含未知文件类型，停止迁移")
    return sorted(result)


def migration_plan(config: dict) -> list[dict]:
    """依据 config 的实际命名模板和输出目录规划旧腾讯目录；查询源站并校验索引，返回不含敏感地址的迁移计划。"""
    root = config["output_path"].resolve()
    parts = list(Formatter().parse(config["naming"]["series_template"]))
    if len(parts) < 3 or parts[0][:2] != ("", "release_date") or parts[1][1] != "title":
        raise ValueError("迁移要求日期位于剧名之前，且模板以 {release_date} 开头")
    separator = parts[1][0]
    markers = [literal for literal, field, _, _ in parts if field == "series_id"]
    if not separator or len(markers) != 1 or not markers[0] or parts[-1][1] != "series_id":
        raise ValueError("迁移要求模板明确分隔日期、剧名和末尾整剧编号")
    matcher = re.compile(re.escape(markers[0]) + r"([A-Za-z0-9]+)$")
    plan = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not (match := matcher.search(folder.name)):
            continue
        if (folder.is_symlink() or getattr(folder.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or folder.resolve().parent != root):
            raise ValueError("腾讯剧目录不在输出根目录内")
        index = folder / "video_info.json"
        if not index.is_file() and re.match(r"^\d{4}-\d{2}-\d{2}" + re.escape(separator), folder.name):
            continue
        data = json.loads(index.read_text(encoding="utf-8"))
        rows = data.get("files") if isinstance(data, dict) and data.get("version") == 1 else None
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"腾讯剧目录索引缺失或为空：{folder.name}")
        video_id = rows[0].get("video_id")
        if not isinstance(video_id, str) or re.fullmatch(r"[A-Za-z0-9]+", video_id) is None:
            raise ValueError(f"腾讯剧目录索引缺少有效视频编号：{folder.name}")
        series_id = match[1]
        title, day = album_date(config, series_id, video_id)
        clean_title = re.sub(r'[\s<>:"/\\|?*\x00-\x1f]+', "_", title).strip(". _")
        new_name = series_directory_name(config, clean_title, series_id, day)
        expected_old = new_name.removeprefix(day + separator)
        if folder.name == new_name:
            continue
        if expected_old == new_name or folder.name != expected_old:
            raise ValueError(f"旧目录名称与源站剧名和当前模板不一致：{folder.name}")
        target = root / new_name
        if target.exists() or target.resolve().parent != root:
            raise FileExistsError(f"目标剧目录已存在或越界：{new_name}")
        files = snapshot(folder)
        names = {item[0] for item in files}
        for item in rows:
            name = item.get("file") if isinstance(item, dict) else None
            if not isinstance(name, str) or Path(name).name != name or name not in names:
                raise ValueError(f"腾讯剧目录索引包含缺失或非法文件：{folder.name}")
        plan.append({"old": folder, "new": target, "series": title, "series_id": series_id, "release_date": day,
                     "files": files, "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest()})
    return plan


def update_history(data: dict, plan: list[dict]) -> dict:
    """将 data 中指向 plan 旧目录的输出和详情路径改为新目录；返回独立副本，其他历史内容保持。"""
    result = deepcopy(data)
    if not isinstance(result, dict) or not isinstance(result.get("runs"), list):
        raise ValueError("腾讯运行历史格式无效")
    for run in result["runs"]:
        if not isinstance(run, dict) or not isinstance(run.get("entries"), list):
            raise ValueError("腾讯运行历史条目无效")
        for entry in run["entries"]:
            for key in ("output", "detail"):
                value = entry.get(key)
                if not isinstance(value, str) or not Path(value).is_absolute():
                    continue
                path = Path(value)
                if path.drive and not path.drive.startswith("\\\\"):
                    path = path.resolve()  # 将映射盘符转换为与输出根目录一致的 UNC 路径。
                for item in plan:
                    if path.is_relative_to(item["old"]):
                        entry[key] = str(item["new"] / path.relative_to(item["old"]))
                        break
    return result


def migrate_existing(config: dict, *, plan: list[dict] | None = None) -> list[dict]:
    """按 config 迁移既有腾讯目录；plan 可传已预检计划，锁内复核并更新历史，失败时回滚，返回迁移结果。"""
    root = config["output_path"].resolve()
    plan = migration_plan(config) if plan is None else plan
    state = root / config["logging"]["state_file"]
    old_bytes = state.read_bytes() if state.exists() else None
    old_history = json.loads(old_bytes.decode("utf-8-sig")) if old_bytes is not None else None
    new_history = update_history(old_history, plan) if old_history is not None else None
    with output_lock(root):
        current_bytes = state.read_bytes() if state.exists() else None
        if current_bytes != old_bytes:
            raise RuntimeError("迁移预检后运行历史发生变化，停止以免覆盖")
        moved, changed_history = [], False
        try:
            for item in plan:
                if (item["old"].is_symlink() or getattr(item["old"].lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                        or item["old"].resolve().parent != root or item["new"].resolve().parent != root
                        or item["new"].exists() or snapshot(item["old"]) != item["files"]):
                    raise RuntimeError("迁移前目录内容或目标发生变化")
                item["old"].rename(item["new"])
                moved.append(item)
                if snapshot(item["new"]) != item["files"] or hashlib.sha256((item["new"] / "video_info.json").read_bytes()).hexdigest() != item["index_sha256"]:
                    raise RuntimeError("迁移后文件或索引快照不一致")
            if new_history is not None and new_history != old_history:
                atomic_json(state, new_history)
                changed_history = True
        except BaseException:
            if changed_history:
                atomic_json(state, old_history)
            for item in reversed(moved):
                if item["old"].exists() or item["new"].resolve().parent != root:
                    raise RuntimeError("迁移回滚目标发生变化，保留现场")
                item["new"].rename(item["old"])
            raise
        return [{key: str(item[key]) for key in ("old", "new", "series", "series_id", "release_date")} for item in plan]
