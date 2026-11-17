"""在共用 video_info.json 中管理链接成品，并原子合并旧逐集记录。"""

from copy import deepcopy
from pathlib import Path

from ..records import DirectoryIndex
from ..storage import output_lock, read_json, valid_digest


class PlaybackDirectoryIndex(DirectoryIndex):
    """复用目录索引的边界检查和原子写入；playback_files 按文件名保存完整链接报告，保留 QSV 分区。"""

    def _validate(self) -> None:
        """核对共用索引及链接分区中的文件名、来源和全片验证证明；无参数及返回值，损坏时保留原文件。"""
        super()._validate()
        records = self.data.get("playback_files", {})
        if not isinstance(records, dict):
            raise ValueError(f"链接成品索引格式无效：{self.path}")
        names = {entry["path"].casefold() for entry in self.data["files"]}
        for name, report in records.items():
            self.validate_record(name, report)
            if name.casefold() in names:
                raise ValueError(f"成品索引存在重复文件名：{name}")
            names.add(name.casefold())

    def validate_record(self, name: str, report: dict) -> None:
        """核对本目录文件名 name 与完整链接报告 report；拒绝路径越界及缺少成功证明的记录，无返回值。"""
        if (not isinstance(name, str) or Path(name).name != name or Path(name).drive
                or Path(name).suffix.lower() != ".mp4" or not isinstance(report, dict)
                or report.get("record_type") != "playback_download" or report.get("schema_version") != 1):
            raise ValueError(f"链接成品记录格式或版本无效：{name}")
        candidate = (self.folder / name).resolve()
        saved = report.get("output")
        if (candidate.parent != self.folder or not isinstance(saved, str) or not Path(saved).is_absolute()
                or Path(saved).resolve() != candidate):
            raise ValueError(f"链接成品记录路径越界或与文件不一致：{name}")
        verification, expected = report.get("verification"), report.get("output_fingerprint")
        if (report.get("status") != "completed" or report.get("media_verified") is not True
                or not isinstance(report.get("source_identity"), dict)
                or not isinstance(verification, dict) or verification.get("decode_exit_code") != 0
                or type(verification.get("frames")) is not int or verification["frames"] <= 0
                or not isinstance(expected, dict) or not valid_digest(expected.get("sha256"))
                or type(expected.get("size_bytes")) is not int or expected["size_bytes"] <= 0):
            raise ValueError(f"链接成品记录缺少完整验证证明：{name}")

    def add(self, name: str, report: dict) -> None:
        """将文件名 name 的完整成功报告 report 加入内存；相同记录幂等，不同记录或 QSV 同名时拒绝覆盖，无返回值。"""
        self.validate_record(name, report)
        records = self.data.setdefault("playback_files", {})
        known = next((saved for saved in records if saved.casefold() == name.casefold()), None)
        if known is not None:
            if known != name or records[known] != report:
                raise ValueError(f"链接成品记录与目录索引冲突：{name}")
            return
        if any(entry["path"].casefold() == name.casefold() for entry in self.data["files"]):
            raise ValueError(f"链接成品与 QSV 索引文件名冲突：{name}")
        records[name] = deepcopy(report)

    def records(self) -> dict:
        """返回按成品文件名索引的链接报告；无参数，缺少链接分区时返回空字典，不修改索引。"""
        return self.data.get("playback_files", {})


def consolidate_directory(folder: Path, root: Path, config: dict) -> int:
    """合并 root 内 folder 的旧逐集 JSON；config 提供写盘重试，调用方须持有输出锁；返回已清理数量。

    仅校验媒体路径、存在性及长度，不在元数据整理时重新读取整片；复用时仍需重算摘要和探测媒体。
    全部记录验证并写入索引、回读核对及确认旧文件未变后才删除冗余 JSON；任一步失败均保留未删除的旧记录。
    """
    index = PlaybackDirectoryIndex(folder, root)
    snapshots = {}
    for path in sorted(index.folder.glob("*.playback.json")):
        if not path.is_file() or path.resolve().parent != index.folder:
            raise ValueError(f"旧链接记录路径越界或不是普通文件：{path}")
        content = path.read_bytes()
        report = read_json(path)
        name = path.name.removesuffix(".playback.json") + ".mp4"
        index.add(name, report)
        media = index.folder / name
        if not media.is_file() or media.stat().st_size != report["output_fingerprint"]["size_bytes"]:
            raise ValueError(f"旧链接成品缺失或长度变化，保留原记录：{media}")
        snapshots[path] = content
    if not snapshots:
        return 0
    index.save(retry_settings=config["logging"])
    if read_json(index.path) != index.data:
        raise RuntimeError("合并后的链接目录索引回读不一致，保留旧记录")
    if any(path.read_bytes() != content for path, content in snapshots.items()):
        raise RuntimeError("合并期间旧链接记录发生变化，保留旧记录")
    for path in snapshots:
        path.unlink()
    return len(snapshots)


def consolidate_output(root: Path, config: dict) -> dict[str, int]:
    """整理 root 内已有的逐集链接记录；config 提供写盘重试和本平台锁名，自动持锁；返回各目录清理数量。"""
    root = root.resolve()
    folders = sorted({path.parent for path in root.rglob("*.playback.json")})
    if not folders:
        return {}
    with output_lock(root, config["logging"]["lock_file"]):
        return {str(folder): consolidate_directory(folder, root, config) for folder in folders}
