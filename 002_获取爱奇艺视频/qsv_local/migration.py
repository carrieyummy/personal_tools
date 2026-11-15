"""将旧单文件模式的已验证成品迁入批量布局，保留可重试的迁移进度。"""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .process import probe_media
from .storage import atomic_json, fingerprint, publish, read_json, valid_digest


def migrate(config: dict, task, index, source_fp: dict, run_id: str) -> bool:
    """按 config 和 task 导入旧根层成功报告到 index；source_fp 为当前源指纹，run_id 标识本批次。
    返回是否完成迁移；先登记新位置再删除旧位置，部分失败由旧报告内的迁移进度恢复。
    """
    report_path = config["work_dir"] / "report.json"
    if not report_path.exists():
        return False
    report = read_json(report_path)
    if report.get("status") != "verified":
        return False
    if not isinstance(report.get("source"), str) or Path(report["source"]).resolve() != task.source:
        return False
    if (not valid_digest(report.get("source_sha256")) or not valid_digest(report.get("output_sha256"))
            or report.get("source_unchanged") is not True
            or report.get("verification", {}).get("decode_exit_code") != 0
            or report.get("verification", {}).get("frames", 0) <= 0):
        raise ValueError(f"旧报告缺少完整成功证明，保留原文件：{report_path}")
    old = Path(report["output"]).resolve()
    if not old.is_relative_to(config["output_path"]) or not old.is_file() and not report.get("migration"):
        if not old.is_relative_to(config["output_path"]):
            raise ValueError(f"旧成品超出输出根目录，不自动移动：{old}")
        return False
    buffer_bytes = config["copy_buffer_bytes"]
    if not report.get("migration"):
        actual = fingerprint(old, buffer_bytes, force=True)
        if actual["sha256"] != report["output_sha256"]:
            raise ValueError(f"旧 MP4 与成功报告不符，保留原文件：{old}")
        destination = task.target if old == task.target else index.destination(task.relative.as_posix(), task.target)
        archive = config["work_dir"] / run_id / "legacy" / task.relative
        report["migration"] = {"id": uuid4().hex, "target": str(destination), "archive_dir": str(archive)}
        atomic_json(report_path, report)
    progress = report["migration"]
    target, archive = Path(progress["target"]).resolve(), Path(progress["archive_dir"]).resolve()
    if target.parent != task.target.parent or not archive.is_relative_to(config["work_dir"]):
        raise ValueError("旧报告迁移路径超出本视频的输出或工作目录")
    if target.exists():
        output_fp = fingerprint(target, buffer_bytes, force=True)
        if output_fp["sha256"] != report["output_sha256"]:
            raise FileExistsError(f"迁移目标与旧报告内容不符，拒绝覆盖：{target}")
    else:
        output_fp = publish(old, target, report["output_sha256"], buffer_bytes)
    archive.mkdir(parents=True, exist_ok=True)
    if "media" not in progress:
        progress["media"] = probe_media(config, target, archive / f"media_{uuid4().hex}.log")
        atomic_json(report_path, report)
    existing = next((entry for entry in index.data["files"] if entry["id"] == progress["id"]), None)
    if existing is None:
        before = deepcopy(index.data)
        try:
            historic = source_fp if source_fp["sha256"] == report["source_sha256"] else {
                "sha256": report["source_sha256"], "size_bytes": None, "mtime_ns": None,
            }
            imported = {**report, "work_dir": str(archive)}
            entry = index.add_file(task.relative.as_posix(), historic, target, output_fp, imported,
                                   progress["media"], identity=progress["id"])
            index.result(task.relative.as_posix(), run_id, "migrated", file_id=entry["id"], work_dir=str(archive))
        except BaseException:
            index.data = before
            raise
    # 新位置及目录索引均已持久化，才删除旧路径；删除失败可利用迁移标识安全重试。
    if old != target:
        old.unlink(missing_ok=True)
    for name in ("mux.log", "decode.log", "decode_progress.log"):
        original, archived = config["work_dir"] / name, archive / name
        if not original.exists():
            continue
        if archived.exists():
            if original.read_bytes() != archived.read_bytes():
                raise FileExistsError(f"迁移日志同名但内容不同：{archived}")
        else:
            original.rename(archived)
        original.unlink(missing_ok=True)
    atomic_json(archive / "report.json", report)
    report_path.unlink()
    return True
