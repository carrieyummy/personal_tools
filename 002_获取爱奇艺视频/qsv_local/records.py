"""管理每个输出目录的成品版本及独立转换结果，不依赖批次日志判断成功。"""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .storage import atomic_json, fingerprint, read_json, time_tag, timestamp, valid_digest


class DirectoryIndex:
    """持有单个输出目录的 video_info.json，历史成品与每次转换结果分别保存。"""

    def __init__(self, folder: Path, output_root: Path):
        """加载输出边界 output_root 内的 folder；缺记录时建内存索引，损坏记录明确报错，不写盘。"""
        self.folder = folder.resolve()
        if not self.folder.is_relative_to(output_root):
            raise ValueError(f"视频目录超出输出根目录：{folder}")
        self.path = self.folder / "video_info.json"
        if not self.path.resolve().is_relative_to(output_root):
            raise ValueError(f"索引链接超出输出根目录：{self.path}")
        self.data = read_json(self.path) if self.path.exists() else {
            "schema_version": 1, "record_type": "converted", "created_at": timestamp(),
            "updated_at": timestamp(), "sources": {}, "files": [], "conversion_results": [],
        }
        self._validate()

    def _validate(self) -> None:
        """验证内存索引的版本、唯一标识、路径和成功证明；无参数或返回值，失败时保留原记录。"""
        data = self.data
        if (data.get("schema_version") != 1 or data.get("record_type") != "converted"
                or not isinstance(data.get("sources"), dict) or not isinstance(data.get("files"), list)
                or not isinstance(data.get("conversion_results"), list)):
            raise ValueError(f"视频索引格式无效：{self.path}")
        identities, paths = set(), set()
        for entry in data["files"]:
            if not isinstance(entry, dict):
                raise ValueError(f"视频索引条目无效：{self.path}")
            name, identity = entry.get("path"), entry.get("id")
            relative = entry.get("source_relative")
            if (not isinstance(identity, str) or not identity or identity in identities
                    or not isinstance(name, str) or Path(name).name != name or Path(name).drive
                    or name in (".", "..") or Path(name).suffix.lower() != ".mp4"
                    or name.casefold() in paths or not isinstance(relative, str)
                    or Path(relative).is_absolute() or Path(relative).drive or ".." in Path(relative).parts
                    or Path(relative).suffix.lower() != ".qsv"
                    or not valid_digest(entry.get("source_sha256"))
                    or not isinstance(entry.get("output_fingerprint"), dict)
                    or not valid_digest(entry["output_fingerprint"].get("sha256"))
                    or entry.get("status") != "completed"
                    or not isinstance(entry.get("verification"), dict)
                    or entry["verification"].get("decode_exit_code") != 0
                    or type(entry["verification"].get("frames")) is not int
                    or entry["verification"]["frames"] <= 0):
                raise ValueError(f"视频索引条目格式或路径无效：{self.path}")
            identities.add(identity)
            paths.add(name.casefold())
        for result in data["conversion_results"]:
            if (not isinstance(result, dict) or not isinstance(result.get("id"), str)
                    or result.get("status") not in {"completed", "reused", "failed", "interrupted", "migrated"}):
                raise ValueError(f"转换结果条目无效：{self.path}")

    def save(self) -> None:
        """原子保存本目录索引；失败传播给调度器，无参数及返回值。"""
        self._validate()
        self.data["updated_at"] = timestamp()
        atomic_json(self.path, self.data)

    def reusable(self, source_relative: str, source_fp: dict, buffer_bytes: int) -> dict | None:
        """为 source_relative/source_fp 寻找有效历史版本；buffer_bytes 为校验块长，返回条目或 None，更新内存缓存。"""
        for entry in reversed(self.data["files"]):
            if (entry["source_relative"] != source_relative or entry["source_sha256"] != source_fp["sha256"]):
                continue
            target = (self.folder / entry["path"]).resolve()
            if target.parent != self.folder or not target.is_file():
                continue
            try:
                actual = fingerprint(target, buffer_bytes, entry["output_fingerprint"])
            except (OSError, ValueError, RuntimeError):
                continue
            if actual["sha256"] == entry["output_fingerprint"]["sha256"]:
                entry["output_fingerprint"] = actual
                return entry
        return None

    def destination(self, source_relative: str, base: Path) -> Path:
        """为逻辑源 source_relative 分配 base 或带时间戳的新路径；未知同名首次成品报冲突，历史名字不复用。"""
        known = {entry["path"].casefold() for entry in self.data["files"]}
        versions = [entry for entry in self.data["files"] if entry["source_relative"] == source_relative]
        if base.exists() and base.name.casefold() not in known:
            raise FileExistsError(f"MP4 已存在但没有成功记录，拒绝覆盖：{base}")
        if not versions and base.name.casefold() not in known:
            return base
        tag, number = time_tag(), 0
        while True:
            suffix = "" if number == 0 else f"_{number:02d}"
            target = base.with_name(f"{base.stem}_{tag}{suffix}.mp4")
            if not target.exists() and target.name.casefold() not in known:
                return target
            number += 1

    def add_file(self, relative: str, source_fp: dict, target: Path, output_fp: dict,
                 report: dict, media: dict, *, identity: str | None = None) -> dict:
        """登记已验证成品；relative/source_fp 描述来源，target/output_fp 描述成品，report/media 为验证和媒体信息；
        identity 可指定迁移稳定标识；返回成品条目，仅更新内存。
        """
        identity = identity or uuid4().hex
        if target.resolve().parent != self.folder:
            raise ValueError(f"成品不在索引目录内：{target}")
        existing = next((item for item in self.data["files"] if item["id"] == identity), None)
        if existing:
            if existing["path"] != target.name or existing["source_sha256"] != source_fp["sha256"]:
                raise ValueError("迁移标识与已登记成品不一致")
            return existing
        entry = {
            "id": identity, "source_relative": relative, "source_sha256": source_fp["sha256"],
            "source_fingerprint": deepcopy(source_fp), "path": target.name, "file_name": target.name,
            "status": "completed", "output_fingerprint": deepcopy(output_fp),
            "saved_at": timestamp(), "media": media, "verification": report.get("verification", {}),
            "conversion": report.get("conversion", {}), "license_version": report.get("license_version"),
            "work_dir": report.get("work_dir", ""),
            "format": report.get("format"),
        }
        self.data["files"].append(entry)
        return entry

    def result(self, relative: str, run_id: str, status: str, *, file_id: str | None = None,
               error: str = "", work_dir: str = "", started_at: str | None = None) -> None:
        """追加一次 relative 在 run_id 的 status 结果；其余参数记录成品、错误、工作目录及开始时间；原子保存，失败恢复内存。"""
        before = deepcopy(self.data)
        self.data["conversion_results"].append({
            "id": uuid4().hex, "run_id": run_id, "source_relative": relative, "status": status,
            "file_id": file_id, "started_at": started_at or timestamp(), "finished_at": timestamp(),
            "errors": [error] if error else [], "work_dir": work_dir,
        })
        try:
            self.save()
        except BaseException:
            self.data = before
            raise
