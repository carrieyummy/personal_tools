"""安全整理已存在的芒果电视剧目录，并同步已登记成品的相对路径。"""

from copy import deepcopy
from pathlib import Path
import re
from string import Formatter
from typing import Any
from uuid import uuid4

from .errors import ToolError
from .series import date_prefix
from .storage import DirectoryLock, atomic_json, read_json, safe_name


def series_pattern(template: str) -> re.Pattern:
    """根据目录 template 构造剧名和数字 clip_id 的完整匹配；模板字段须各出现一次，返回正则对象。"""
    parts, fields = [], []
    for literal, field, spec, conversion in Formatter().parse(template):
        parts.append(re.escape(literal))
        if field is not None:
            if field not in ("title", "clip_id") or field in fields or spec or conversion:
                raise ToolError("整理旧目录时剧集模板必须只含一次未格式化的 title 和 clip_id")
            fields.append(field)
            parts.append(r"(?P<title>.+?)" if field == "title" else r"(?P<clip_id>[0-9]+)")
    if set(fields) != {"title", "clip_id"}:
        raise ToolError("整理旧目录时剧集模板缺少 title 或 clip_id")
    return re.compile("".join(parts))


def find_series(config: dict[str, Any]) -> list[dict[str, str]]:
    """按 config 的输出根目录和命名模板发现芒果剧集目录；返回目录名及编号，不移动或修改文件。"""
    root, naming = config["output_path"].resolve(), config["naming"]
    matcher = series_pattern(naming["series_template"])
    prefix = naming["series_date_prefix"]
    marker = "0000-00-00"
    rendered = prefix.format_map({"release_date": marker})
    if rendered.count(marker) != 1:
        raise ToolError("日期前缀中无法唯一识别日期位置")
    before, after = rendered.split(marker)
    existing_prefix = re.compile(re.escape(before) + r"\d{4}-\d{2}-\d{2}" + re.escape(after))
    result = []
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        match_prefix = existing_prefix.match(folder.name)
        stem = folder.name[match_prefix.end():] if match_prefix else folder.name
        match = matcher.fullmatch(stem)
        if not match:
            continue
        if folder.is_symlink() or folder.is_junction() or folder.resolve().parent != root:
            raise ToolError("芒果电视剧目录不是输出根目录内的普通目录")
        result.append({"name": folder.name, "stem": stem, "clip_id": match["clip_id"]})
    return sorted(result, key=lambda item: item["name"])


def plan_series_dates(config: dict[str, Any], client) -> list[dict[str, str]]:
    """按 config 查询 client 的官方剧名与上映日，规划已有目录的新名称；返回所有匹配条目，不执行移动。"""
    result, targets = [], set()
    naming = config["naming"]
    for item in find_series(config):
        clip_id = item["clip_id"]
        info = client.catalog_page(clip_id, 1)["clipInfo"]
        if str(info["clipId"]) != clip_id or not isinstance(info.get("clipName"), str) or not info["clipName"]:
            raise ToolError("旧目录的官方剧集编号或剧名无效")
        expected = naming["series_template"].format_map({"title": safe_name(info["clipName"], naming["max_title_length"]), "clip_id": clip_id})
        if item["stem"] != expected:
            raise ToolError(f"旧目录名称与官方资料或当前模板不一致，保留现场：{item['name']}")
        release_date = client.series_release_date(clip_id)
        target = date_prefix(naming, release_date) + item["stem"]
        if len(target) > naming["max_filename_length"] or safe_name(target, naming["max_filename_length"]) != target:
            raise ToolError("日期目录名不符合当前命名配置")
        if target.casefold() in targets:
            raise ToolError("多个芒果目录将映射到同一目标")
        targets.add(target.casefold())
        result.append({**item, "target": target, "release_date": release_date})
    return result


def snapshot_tree(folder: Path) -> dict[str, tuple[int, int]]:
    """读取 folder 内文件的相对名称、大小及修改时间；拒绝链接，返回供原位改名比对的快照。"""
    result = {}
    for path in folder.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise ToolError(f"目录内存在链接，不能自动整理：{path}")
        if path.is_file():
            stat = path.stat()
            result[path.relative_to(folder).as_posix()] = (stat.st_size, stat.st_mtime_ns)
        elif not path.is_dir():
            raise ToolError(f"目录内存在不支持的文件类型：{path}")
    return result


def synchronize_series(config: dict[str, Any], plan: list[dict[str, str]]) -> dict[str, Any]:
    """按 plan 原位重命名 config 的剧集目录并同步已有索引；取得专用锁、保留事务记录；返回改名及索引统计。"""
    root = config["output_path"].resolve()
    work = config["work_dir"].resolve()
    settings = config["logging"]
    with DirectoryLock(root / settings["lock_file"]), DirectoryLock(work / settings["work_lock_file"]):
        current = {item["name"]: item for item in find_series(config)}
        if set(current) != {item["name"] for item in plan}:
            raise ToolError("规划后芒果目录发生变化，请重新规划")
        moves, snapshots, targets = [], {}, set()
        for item in plan:
            if current[item["name"]]["clip_id"] != item["clip_id"] or current[item["name"]]["stem"] != item["stem"]:
                raise ToolError("规划后芒果目录身份发生变化")
            expected = date_prefix(config["naming"], item["release_date"]) + item["stem"]
            if item["target"] != expected or Path(item["target"]).name != item["target"]:
                raise ToolError("计划中的目标名称不符合上映日期")
            source, target = root / item["name"], root / item["target"]
            if source.resolve().parent != root or target.resolve().parent != root:
                raise ToolError("目录改名目标不在指定输出根目录")
            if target.name.casefold() in targets:
                raise ToolError("目录改名目标重复")
            targets.add(target.name.casefold())
            if source == target:
                continue
            if target.exists():
                raise ToolError(f"目标目录已存在，未覆盖：{target}")
            snapshots[source.name] = snapshot_tree(source)
            moves.append((source, target))
        index_path = root / settings["index_file"]
        if index_path.is_symlink() or index_path.resolve().parent != root:
            raise ToolError("芒果成品索引不在输出根目录")
        original = read_json(index_path, None)
        index = deepcopy(original)
        changed = 0
        if index is not None:
            if not isinstance(index, dict) or index.get("schema") != 1 or not isinstance(index.get("videos"), dict):
                raise ToolError("芒果成品索引格式无效，未更改目录")
            mapping = {source.name: target.name for source, target in moves}
            for versions in index["videos"].values():
                if not isinstance(versions, list):
                    raise ToolError("芒果成品索引版本格式无效")
                for record in versions:
                    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                        raise ToolError("芒果成品索引路径无效")
                    first, separator, remainder = record["path"].partition("/")
                    if first in mapping:
                        if not separator or not (root / record["path"]).resolve().is_relative_to(root / first):
                            raise ToolError("芒果成品索引路径越出原剧集目录")
                        record["path"] = mapping[first] + "/" + remainder
                        changed += 1
        if not moves:
            return {"moved": [], "updated_records": 0, "index_present": original is not None}
        journal = work / ("series_date_sync_" + uuid4().hex + ".json")
        transaction = {"status": "prepared", "moves": [{"source": a.name, "target": b.name} for a, b in moves], "original_index": original}
        atomic_json(journal, transaction, settings)
        index_written = False
        try:
            for source, target in moves:
                source.rename(target)
                if snapshot_tree(target) != snapshots[source.name]:
                    raise ToolError("目录改名后文件清单或属性发生变化")
            if changed:
                atomic_json(index_path, index, settings)
                index_written = True
            transaction["status"] = "completed"
            atomic_json(journal, transaction, settings)
        except BaseException:
            rollback_errors = []
            for source, target in reversed(moves):
                if target.exists() and not source.exists():
                    try:
                        target.rename(source)
                    except OSError as error:
                        rollback_errors.append(f"{target.name}: {type(error).__name__}")
                elif target.exists() == source.exists():
                    rollback_errors.append(f"{source.name}: 源和目标状态异常")
            if index_written:
                try:
                    atomic_json(index_path, original, settings)
                except OSError as error:
                    rollback_errors.append(f"索引恢复: {type(error).__name__}")
            transaction.update(status="rollback_failed" if rollback_errors else "rolled_back", rollback_errors=rollback_errors)
            atomic_json(journal, transaction, settings)
            if rollback_errors:
                raise ToolError(f"目录整理失败且回滚不完整，请检查事务记录：{journal}") from None
            raise
        return {"moved": [{"source": a.name, "target": b.name, "files": len(snapshots[a.name])} for a, b in moves],
                "updated_records": changed, "index_present": original is not None, "journal": str(journal)}
