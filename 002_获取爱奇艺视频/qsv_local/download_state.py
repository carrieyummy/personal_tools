"""只读核对爱奇艺客户端的完成记录，在读取视频内容前排除未完成或状态不明的文件。"""

from pathlib import Path
import re
import sqlite3
from xml.etree import ElementTree as ET


class DownloadNotReady(RuntimeError):
    """表示客户端未确认下载完成；调用方应跳过该视频，不将其视为可转换内容。"""


def require_completed(config: dict, source: Path, *, expected: tuple | None = None) -> None:
    """按 config 核对 source 状态及可选 expected 命名元数据；不一致时抛 DownloadNotReady，无返回值。"""
    status = DownloadStatus(config)
    reason = status.reason(source)
    if reason:
        raise DownloadNotReady(f"{source.name}：{reason}")
    if expected is not None and status.metadata.get(path_key(source)) != expected:
        raise DownloadNotReady(f"{source.name}：客户端下载命名元数据发生变化")


def path_key(path: Path) -> str:
    """将 path 规范化为大小写不敏感的绝对路径标识；只解析路径，不读取文件内容。"""
    return str(path.resolve()).casefold()


class DownloadStatus:
    """读取客户端下载元数据；进行中记录优先于完成记录，无法确认的文件一律不进入转换。"""

    def __init__(self, config: dict):
        """保存 config 中的 download_guard 设置；构造时不接触视频或数据库，无返回值。"""
        self.settings = config["download_guard"]
        self.completed, self.active, self.metadata = set(), set(), {}

    def refresh(self) -> None:
        """以 SQLite 只读事务加载完成及进行中 XML；编码按配置读取，未知格式报错，不创建或修复客户端文件。"""
        settings = self.settings
        connection = sqlite3.connect(settings["database_path"].as_uri() + "?mode=ro", uri=True,
                                     timeout=settings["database_timeout_seconds"])
        try:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                "SELECT Name, Data FROM table_file WHERE Name=? COLLATE NOCASE OR Name=? COLLATE NOCASE",
                (settings["completed_record"], settings["active_record"]),
            ).fetchall()
        finally:
            connection.close()
        records = {name.casefold(): data for name, data in rows}
        required = (settings["completed_record"], settings["active_record"])
        if any(name.casefold() not in records for name in required):
            raise ValueError("客户端下载数据库缺少完成或进行中记录，禁止读取视频")
        completed, active, metadata = set(), set(), {}
        for record, destination in ((settings["completed_record"], completed), (settings["active_record"], active)):
            blob = records[record.casefold()]
            if not isinstance(blob, bytes):
                raise ValueError(f"客户端下载记录不是 XML BLOB：{record}")
            text = blob.decode(settings["xml_encoding"])
            # 字节已经按明确配置解码；移除可能错误的 XML 编码声明，避免解析器再次解释多字节编码。
            text = re.sub(r"^\ufeff?\s*<\?xml[^>]*\?>", "", text, count=1)
            root = ET.fromstring(text)
            if root.tag != "Root":
                raise ValueError(f"客户端下载 XML 根节点无效：{record}")
            for item in root.findall("./Chs/Ch"):
                folder, name = item.get("SaveDir"), item.get("SaveFileName")
                if (not folder or not name or not Path(folder).is_absolute() or Path(name).name != name
                        or Path(name).drive or name in (".", "..")):
                    raise ValueError(f"客户端下载记录路径无效：{record}")
                if destination is completed and item.get("State") not in {
                    str(state) for state in settings["completed_states"]
                }:
                    continue
                key = path_key(Path(folder) / name)
                destination.add(key)
                if destination is completed:
                    values = (item.get("AlbumID"), item.get("AlbumName"), item.get("vedioorder"))
                    if key in metadata and metadata[key] != values:
                        raise ValueError(f"同一下载路径存在冲突的命名元数据：{name}")
                    metadata[key] = values
        self.completed, self.active, self.metadata = completed, active, metadata

    def reason(self, source: Path, *, refresh: bool = True) -> str | None:
        """检查 source 是否明确完成；refresh 决定是否重新查询数据库，返回不可处理原因，完成时返回 None。

        只检查元数据及临时文件是否存在，不打开、哈希、改名、锁定或删除视频。
        """
        if refresh:
            self.refresh()
        key = path_key(source)
        if key in self.active:
            return "客户端记录仍在下载或等待下载"
        if key not in self.completed:
            return "客户端没有明确的下载完成记录"
        for suffix in self.settings["temporary_suffixes"]:
            if source.with_name(source.name + suffix).exists():
                return "存在同名下载临时文件"
        return None
