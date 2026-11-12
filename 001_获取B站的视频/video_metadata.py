"""维护投稿目录唯一的 video_info.json，供下载续传、媒体信息展示和无损拼接共同使用。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
from typing import Any


class ToolError(RuntimeError):
    """表示可直接向用户解释的配置、登录、下载、元数据或封装错误。"""


def timestamp(value: float | None = None) -> str:
    """将 Unix 时间 value（None 表示现在）转为包含本地时区的 ISO 8601 字符串。"""
    instant = datetime.now().astimezone() if value is None else datetime.fromtimestamp(value).astimezone()
    return instant.isoformat(timespec="seconds")


def atomic_text(path: Path, content: str) -> None:
    """将 UTF-8 content 原子写入 path；成功后替换目标，失败时清理临时文件，返回 None。"""
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    """读取 path 中的 JSON 对象；返回字典，损坏或非对象内容抛出 ToolError。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("根节点必须为对象")
        return data
    except (OSError, ValueError) as exc:
        raise ToolError(f"无法读取信息文件 {path}：{exc}") from exc


def probe_media(executable: str, path: Path) -> dict[str, Any]:
    """用 executable 指定的 FFmpeg 快速读取 path 文件头；返回本地媒体参数和缺项原因，不完整解码。"""
    result = subprocess.run(
        [executable, "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:v:0?", "-map", "0:a:0?", "-t", "0", "-c", "copy", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise ToolError("读取媒体信息失败：" + "\n".join(result.stderr.splitlines()[-6:]))
    header = result.stderr.split("Stream mapping:", 1)[0].split("Output #", 1)[0]
    video = next((line for line in header.splitlines() if "Stream #" in line and "Video: " in line), "")
    audio = next((line for line in header.splitlines() if "Stream #" in line and "Audio: " in line), "")
    duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", header)
    # 尺寸前必须有空格，避免将 Stream #0:0[0x1] 等十六进制流标识当成分辨率。
    size = re.search(r" (\d+)x(\d+)(?=[, \[]|$)", video)
    fps = re.search(r"\b([\d.]+) fps\b", video)
    data: dict[str, Any] = {
        "duration_seconds": int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3]) if duration else None,
        "width": int(size[1]) if size else None,
        "height": int(size[2]) if size else None,
        "fps": float(fps[1]) if fps else None,
        "video_codec": video.split("Video: ", 1)[1].split()[0].rstrip(",") if video else None,
        "audio_codec": audio.split("Audio: ", 1)[1].split()[0].rstrip(",") if audio else None,
    }
    data["errors"] = [f"本地文件未提供 {key}" for key, value in data.items() if value is None]
    return data


class VideoInfo:
    """管理单个投稿的唯一记录；所有媒体路径均相对此文件，解析后必须位于配置下载目录中。"""

    def __init__(self, output_dir: Path, folder: Path, bvid: str, title: str, total_parts: int | None = None) -> None:
        """以 output_dir 为边界，在 folder 加载或初始化 bvid/title 投稿；total_parts 为已知总分 P 数，无返回值。"""
        self.output_dir = output_dir.resolve()
        self.path = folder.resolve() / "video_info.json"
        if not self.path.is_relative_to(self.output_dir):
            raise ToolError(f"信息文件超出下载目录：{self.path}")
        now = timestamp()
        self.data = read_json(self.path) if self.path.exists() else {
            "schema_version": 1, "bvid": bvid, "title": title,
            "source_url": f"https://www.bilibili.com/video/{bvid}",
            "uploader": None, "total_parts": total_parts, "created_at": now, "updated_at": now,
            "files": [], "errors": ["尚未获取 UP 主信息"] + (["尚未获取总分 P 数"] if total_parts is None else []),
        }
        if self.data.get("schema_version") != 1 or self.data.get("bvid") != bvid or not isinstance(self.data.get("files"), list):
            raise ToolError(f"信息文件版本、BV 号或文件列表无效：{self.path}")
        entries = self.data["files"]
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in entries):
            raise ToolError(f"信息文件的文件条目无效：{self.path}")
        if len({item["id"] for item in entries}) != len(entries):
            raise ToolError(f"信息文件包含重复条目：{self.path}")
        if (not isinstance(self.data.get("title"), str) or not self.data["title"]
                or not isinstance(self.data.get("errors"), list)
                or any(item.get("kind") not in ("part", "merged", "video")
                       or item.get("status") not in ("pending", "downloading", "completed", "failed", "interrupted")
                       or not isinstance(item.get("formats"), list) or not isinstance(item.get("errors"), list) for item in entries)):
            raise ToolError(f"信息文件内容无效：{self.path}")

    def save(self) -> None:
        """原子保存当前信息；成功返回 None，失败抛出异常且保留原文件。"""
        self.data["updated_at"] = timestamp()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_text(self.path, json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")

    def entry(self, entry_id: str, kind: str = "part", part: int | None = None, title: str | None = None) -> dict[str, Any]:
        """获取或创建 entry_id 条目；kind 为 part/merged/video，part 为分 P 序号，title 为标题，返回可更新字典。"""
        for item in self.data["files"]:
            if item["id"] == entry_id:
                return item
        item = {
            "id": entry_id, "kind": kind, "part": part, "title": title,
            "source_url": self.data["source_url"] + (f"?p={part}" if part is not None else ""),
            "status": "pending", "path": None, "file_name": None, "stem": None,
            "formats": [], "processing": None, "size_bytes": None, "saved_at": None,
            "duration_seconds": None, "width": None, "height": None, "fps": None,
            "video_codec": None, "audio_codec": None, "errors": [],
        }
        self.data["files"].append(item)
        return item

    def resolve_file(self, entry: dict[str, Any]) -> Path:
        """将 entry 的相对路径解析为绝对 Path；拒绝绝对路径或下载目录外的文件。"""
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or Path(relative).drive:
            raise ToolError(f"信息文件包含无效媒体路径：{relative}")
        result = (self.path.parent / relative).resolve()
        if not result.is_relative_to(self.output_dir):
            raise ToolError(f"媒体文件超出下载目录：{relative}")
        return result

    def record_file(self, entry: dict[str, Any], path: Path) -> None:
        """将 path 的真实文件属性记录到 entry 并标为完成；无磁盘写入，返回 None。"""
        path = path.resolve()
        if not path.is_relative_to(self.output_dir):
            raise ToolError(f"媒体文件超出下载目录：{path}")
        stat = path.stat()
        if not path.is_file() or not stat.st_size:
            raise ToolError(f"媒体文件不存在或为空：{path}")
        entry.update(path=path.relative_to(self.path.parent, walk_up=True).as_posix(), file_name=path.name,
                     stem=entry.get("stem") or path.stem, size_bytes=stat.st_size,
                     saved_at=timestamp(stat.st_mtime), status="completed")

    def enrich_file(self, entry: dict[str, Any], executable: str) -> None:
        """用 executable 补充 entry 的本地媒体参数；读取失败只记录原因，完成状态不变，返回 None。"""
        for key in ("duration_seconds", "width", "height", "fps", "video_codec", "audio_codec"):
            entry[key] = None
        try:
            entry.update(probe_media(executable, self.resolve_file(entry)))
        except (OSError, ToolError) as exc:
            entry["errors"] = [str(exc)]

    def complete(self, entry: dict[str, Any], path: Path, executable: str) -> None:
        """先持久化 entry/path 完成记录，再用 executable 补充媒体参数并保存；失败时由调用方保留下载流。"""
        self.record_file(entry, path)
        entry["errors"] = []
        self.save()
        self.enrich_file(entry, executable)
        self.save()

    def flatten_single_part(self) -> None:
        """仅当投稿总分 P 数为 1 时将旧 p1 目录内容移至投稿目录并更新路径；冲突拒绝覆盖，保存失败回滚，返回 None。"""
        if self.data.get("total_parts") != 1:
            return
        root = self.path.parent
        source = root / f"{self.data['bvid']}_p1"
        if not source.exists():
            return
        if not source.is_dir() or source.resolve() != source or not source.is_relative_to(self.output_dir):
            raise ToolError(f"单分 P 迁移目录无效：{source}")
        if any(item.get("kind") == "part" and item.get("part") != 1 for item in self.data["files"]):
            raise ToolError("总分 P 数为 1，但记录包含其他分 P，无法迁移目录")
        moves = [(child, root / child.name) for child in sorted(source.iterdir())]
        for old, new in moves:
            if new.exists() or new.is_symlink():
                raise ToolError(f"单分 P 目录迁移存在同名目标，不覆盖：{new}")
            if old.is_symlink() or old.is_junction():
                raise ToolError(f"单分 P 目录包含链接，无法直接迁移：{old}")
        replacements = []
        for item in self.data["files"]:
            if item.get("path"):
                old = self.resolve_file(item)
                if old.is_relative_to(source):
                    replacements.append((item, (root / old.relative_to(source)).relative_to(root).as_posix()))
        original = deepcopy(self.data)
        moved: list[tuple[Path, Path]] = []
        try:
            for old, new in moves:
                old.rename(new)
                moved.append((old, new))
            for item, relative in replacements:
                item["path"] = relative
            self.save()
        except BaseException as exc:
            self.data = original
            for old, new in reversed(moved):
                try:
                    new.rename(old)
                except OSError as rollback_error:
                    exc.add_note(f"目录迁移回滚失败：{new} -> {old}：{rollback_error}")
            raise
        # 非递归删除已搬空的目录；保留文件和断点流的原始内容及修改时间。
        source.rmdir()

    def refresh_local(self, executable: str) -> None:
        """用 executable 更新已登记文件并发现投稿目录内的正式 MP4/MKV；排除临时文件，保存结果，返回 None。"""
        known: set[Path] = set()
        for item in self.data["files"]:
            if item.get("path"):
                path = self.resolve_file(item)
                known.add(path)
                if item.get("status") == "completed":
                    try:
                        self.record_file(item, path)
                        self.enrich_file(item, executable)
                    except (OSError, ToolError) as exc:
                        item.update(status="failed", errors=[str(exc)])
        for path in sorted(self.path.parent.rglob("*")):
            relative = path.relative_to(self.path.parent)
            if not path.is_file() or path.suffix.lower() not in (".mp4", ".mkv") or path.resolve() in known:
                continue
            if any(piece.startswith(".") for piece in relative.parts) or ".partial." in path.name:
                continue
            match = re.fullmatch(re.escape(self.data["bvid"]) + r"_p(\d+)", path.parent.name)
            part = int(match[1]) if match else None
            entry_id = f"{self.data['bvid']}_p{part}" if part else f"video:{relative.as_posix()}"
            kind = "part" if part else "video"
            if any(item["id"] == entry_id for item in self.data["files"]):
                # 同分 P 的其他容器/历史文件只登记为附加视频，不能覆盖已确认的格式签名与路径。
                entry_id, kind = f"video:{relative.as_posix()}", "video"
            item = self.entry(entry_id, kind, part, path.stem)
            # 未经下载记录确认的旧文件不推断 format_id，不能据此跳过新选择的格式。
            self.record_file(item, path)
            self.enrich_file(item, executable)
            if item["video_codec"] is None:
                item["status"] = "failed"
        self.save()


def open_video(output_dir: Path, bvid: str, title: str | None = None, total_parts: int | None = None) -> VideoInfo:
    """在 output_dir 定位 bvid 已有投稿目录；无目录时以 title 创建记录对象，total_parts 为已知总数，返回 VideoInfo。"""
    folders = sorted(path for path in output_dir.glob(f"*_{bvid}") if path.is_dir())
    if len(folders) > 1:
        raise ToolError(f"{bvid} 存在多个投稿目录，请整理后重试：{folders}")
    if folders:
        folder = folders[0]
        title = title or folder.name[:-(len(bvid) + 1)]
    else:
        if not title:
            raise ToolError(f"未找到 {bvid} 的投稿目录")
        # title 由下载入口沿用现有 safe_name 规则清理，迁移入口也必须先清理。
        folder = output_dir / f"{title}_{bvid}"
    return VideoInfo(output_dir, folder, bvid, title, total_parts)


def discover_videos(output_dir: Path) -> tuple[list[str], list[str]]:
    """发现 output_dir 的投稿目录与旧下载记录；返回 BV 号列表及损坏旧记录的错误列表。"""
    ids: set[str] = set()
    errors: list[str] = []
    for folder in output_dir.iterdir() if output_dir.is_dir() else []:
        match = re.search(r"_(BV[0-9A-Za-z]{10})$", folder.name)
        if folder.is_dir() and match:
            ids.add(match[1])
    for path in output_dir.rglob("download.json"):
        try:
            record = read_json(path)
            match = re.fullmatch(r"(BV[0-9A-Za-z]{10})_p\d+", str(record.get("id", "")))
            if not match:
                raise ToolError(f"旧记录缺少合法分 P 标识：{path}")
            ids.add(match[1])
        except ToolError as exc:
            errors.append(str(exc))
    return sorted(ids), errors


def migrate_legacy(info: VideoInfo) -> None:
    """将 info 对应 BV 号的旧 download.json 合并并核对后删除；冲突或保存失败时保留全部旧记录，返回 None。"""
    migrated: list[tuple[Path, dict[str, Any]]] = []
    totals = {info.data["total_parts"]} if info.data.get("total_parts") is not None else set()
    for path in sorted(info.output_dir.rglob("download.json")):
        try:
            record = read_json(path)
        except ToolError:
            if info.data["bvid"] in str(path):
                raise
            continue
        match = re.fullmatch(re.escape(info.data["bvid"]) + r"_p(\d+)", str(record.get("id", "")))
        if not match:
            continue
        if not info.path.exists() and record.get("video_title"):
            info.data["title"] = record["video_title"]
        name = record.get("file")
        formats = record.get("formats")
        if (not isinstance(name, str) or Path(name).name != name or Path(name).drive
                or not isinstance(formats, list) or not formats or any(not isinstance(fmt, dict) for fmt in formats)
                or not isinstance(record.get("stem", Path(name).stem), str)):
            raise ToolError(f"旧下载记录内容无效：{path}")
        total = record.get("total_parts")
        if total is not None:
            if type(total) is not int or total < 1:
                raise ToolError(f"旧记录总分 P 数无效：{path}")
            totals.add(total)
        if len(totals) > 1:
            raise ToolError(f"{info.data['bvid']} 的旧记录总分 P 数不一致")
        source = (path.parent / name).resolve()
        item = info.entry(record["id"], "part", int(match[1]), record.get("title"))
        if item.get("path") and (info.resolve_file(item) != source or item.get("formats") != formats):
            raise ToolError(f"新旧下载记录冲突：{path}")
        item.update(title=record.get("title"), stem=record.get("stem", source.stem),
                    formats=formats, processing=record.get("processing"), errors=[])
        info.record_file(item, source)
        migrated.append((path, dict(item)))
    if not migrated:
        return
    info.data["total_parts"] = next(iter(totals)) if totals else None
    if info.data["total_parts"] is not None:
        info.data["errors"] = [error for error in info.data["errors"] if error != "尚未获取总分 P 数"]
    info.save()
    verified = read_json(info.path)
    for path, item in migrated:
        if item not in verified["files"]:
            raise ToolError(f"旧记录迁移核对失败，保留原记录：{path}")
    for path, _ in migrated:
        path.unlink()
