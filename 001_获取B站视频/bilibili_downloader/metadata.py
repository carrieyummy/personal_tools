"""聚合投稿各目录的视频索引，按目录保存源信息并迁移旧布局，供下载续传和无损拼接复用。"""

from __future__ import annotations

import json
import re
import subprocess
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import ToolError
from .models import LEGACY_QUALITY, Quality, entry_quality, quality_entry_id
from . import runtime
from . import naming
from . import variants
from .storage import record_io_lock


def timestamp(value: float | None = None) -> str:
    """将 Unix 时间 value（None 表示现在）转为包含本地时区的 ISO 8601 字符串。"""
    instant = datetime.now().astimezone() if value is None else datetime.fromtimestamp(value).astimezone()
    return instant.isoformat(timespec="seconds")


def atomic_text(path: Path, content: str) -> None:
    """将 UTF-8 content 原子写入 path；与记录读取协调，避免 Windows 打开句柄阻止替换；失败清理临时文件。"""
    with record_io_lock:
        temporary = path.with_name(path.name + ".tmp")
        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    """读取 path 到内存后关闭句柄，再解析 JSON；与原子替换协调，返回字典，非法内容抛出 ToolError。"""
    try:
        with record_io_lock:
            text = path.read_text(encoding='utf-8')
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("根节点必须为对象")
        return data
    except (OSError, ValueError) as exc:
        raise ToolError(f"无法读取信息文件 {path}：{exc}") from exc


def probe_media(executable: str, path: Path) -> dict[str, Any]:
    """用 executable 指定的 FFmpeg 快速读取 path 文件头；返回本地媒体参数和缺项原因，不完整解码。"""
    result = runtime.run_process(
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
    """管理单个投稿的内存视图；磁盘源索引按目录独立保存，内存媒体路径相对当前锚点且限制在下载根目录内。"""

    def __init__(self, output_dir: Path, folder: Path, bvid: str, title: str, total_parts: int | None = None, *, shared: bool = False) -> None:
        """以 output_dir 为边界加载 folder 中的 bvid/title 投稿；total_parts 为总分 P 数，shared 表示合集共用目录，无返回值。"""
        self.output_dir = output_dir.resolve()
        self.path = folder.resolve() / (f"{bvid}_video_info.json" if shared else "video_info.json")
        self._index_paths = {self.path} if self.path.exists() else set()
        self._quality_folders: dict[int | None, Path] = {}
        if not self.path.is_relative_to(self.output_dir):
            raise ToolError(f"信息文件超出下载目录：{self.path}")
        now = timestamp()
        self.data = read_json(self.path) if self.path.exists() else {
            "schema_version": 7, "record_type": "source", "mix_quality": False, "bvid": bvid, "title": title,
            "source_url": f"https://www.bilibili.com/video/{bvid}",
            "uploader": None, "total_parts": total_parts, "created_at": now, "updated_at": now,
            "files": [], "errors": ["尚未获取 UP 主信息"] + (["尚未获取总分 P 数"] if total_parts is None else []),
        }
        if self.data.get("record_type") == 'merged' or self.data.get("schema_version") not in (1, 2, 3, 4, 5, 6, 7) or self.data.get("bvid") != bvid or not isinstance(self.data.get("files"), list):
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
        if 'current_parts' in self.data:
            pages = self.data['current_parts']
            if (not isinstance(pages, list) or not pages
                    or any(not isinstance(page, dict) or type(page.get('part')) is not int or page['part'] < 1
                           or type(page.get('cid')) is not int or page['cid'] < 1 for page in pages)
                    or sorted(page['part'] for page in pages) != list(range(1, len(pages) + 1))):
                raise ToolError(f'信息文件当前分 P 列表无效：{self.path}')
        if self.data.get('schema_version') in (6, 7) and type(self.data.get('mix_quality')) is not bool:
            raise ToolError(f'源目录索引 mix_quality 必须为布尔值：{self.path}')
        if self.path.exists() and self.data.get('schema_version') in (5, 6, 7):
            if self.data.get('record_type') != 'source':
                raise ToolError(f'源目录索引类型无效：{self.path}')
            if any(item['kind'] == 'merged' or (not self.mix_quality and item.get('quality_id') != self.data.get('quality_id'))
                   or (item.get('path') and (not isinstance(item['path'], str)
                                            or Path(item['path']).name != item['path'] or Path(item['path']).drive
                                            or item['path'] in ('.', '..')))
                   for item in entries):
                raise ToolError(f'源目录索引包含跨目录路径或混合画质：{self.path}')
            if entries:
                self._quality_folders.update({item.get('quality_id'): self.path.parent for item in entries})

    @property
    def mix_quality(self) -> bool:
        """返回磁盘索引声明的存放方式；v1～v5 明确属于旧分画质布局，无参数。"""
        return self.data['mix_quality'] if self.data.get('schema_version') in (6, 7) else False

    @property
    def base_directory(self) -> Path:
        """返回不含清晰度后缀的逻辑投稿/合集路径；目录可不存在，无参数。"""
        name = self.data.get("directory_base")
        if name is None:
            return self.path.parent
        if not isinstance(name, str) or not name or Path(name).name != name or Path(name).drive or name in ('.', '..'):
            raise ToolError(f"投稿基础目录名无效：{name}")
        base = self.path.parent.parent / name
        if not base.resolve().is_relative_to(self.output_dir) or base.is_symlink() or base.is_junction():
            raise ToolError(f"投稿基础目录无效：{base}")
        return base

    @property
    def index_paths(self) -> list[Path]:
        """返回此投稿已有及待写入的全部源索引路径，供跨文件事务完整回滚；无参数。"""
        from .source_records import documents

        planned = documents(self).keys() if self.data.get('schema_version') in (5, 6, 7) else [self.path]
        return sorted(self._index_paths | set(planned))

    def save(self) -> None:
        """保存投稿视图到各目录索引；旧布局在迁移步骤完成前保留旧写入规则，失败恢复原文件，无返回值。"""
        if self.data.get('schema_version') not in (5, 6, 7):
            self._save_legacy()
            self._index_paths = {self.path}
            return
        from .source_records import save_view

        save_view(self)

    def migrate_directory_indexes(self) -> None:
        """只拆分此投稿的源 JSON 到媒体实际目录，保留媒体和指纹，不联网、不移动文件；失败恢复旧视图，无返回值。"""
        original = deepcopy(self.data)
        self.data.update(schema_version=7, mix_quality=self.mix_quality)
        try:
            self.save()
        except BaseException:
            self.data = original
            raise

    def _save_legacy(self) -> None:
        """保存尚未完成布局整理的 v1～v4 单一源记录；只供旧布局迁移过程使用，无参数及返回值。"""
        original, original_path = deepcopy(self.data), self.path
        base = self.base_directory
        destination = self.path
        if self.data['files'] and 'directory_base' not in self.data:
            destination = self.quality_directory(entry_quality(self.data['files'][0])) / self.path.name
        if destination != self.path and (destination.exists() or destination.is_symlink()):
            raise ToolError(f"投稿记录已存在，不覆盖：{destination}")
        try:
            if destination != self.path:
                for item in self.data['files']:
                    if item.get('path'):
                        item['path'] = self.resolve_file(item).relative_to(destination.parent, walk_up=True).as_posix()
                self.data.update(directory_base=base.name, schema_version=4)
            self.data["updated_at"] = timestamp()
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_text(destination, json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")
            if destination != self.path:
                original_path.unlink(missing_ok=True)
                self.path = destination
        except BaseException:
            self.data = original
            if destination != original_path:
                destination.unlink(missing_ok=True)
            raise
        if destination != original_path:
            remove_empty_directories(base, self.output_dir)

    def entry(self, entry_id: str, kind: str = "part", part: int | None = None, title: str | None = None, *,
              quality: Quality = LEGACY_QUALITY, variant: str = '') -> dict[str, Any]:
        """按原始 entry_id、quality 和可选 variant 获取/创建条目；kind/part/title 描述文件，返回可更新字典。"""
        if variant:
            matches = [item for item in self.data['files'] if item.get('kind') == kind and item.get('part') == part
                       and item.get('quality_id') == quality.code and item.get('formats')
                       and variants.variant_id(item['formats']) == variant]
            if len(matches) > 1:
                raise ToolError('同一分 P 的格式版本存在重复记录')
            if matches:
                return matches[0]
        entry_id = variants.source_id(entry_id, quality, variant)
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
            "quality_id": quality.code, "quality_name": quality.name,
            "quality_dir": self.quality_suffix(quality),
            "format_variant": variant or None,
        }
        self.data["files"].append(item)
        return item

    def quality_suffix(self, quality: Quality) -> str:
        """取得 quality 对应的清晰度后缀；复用已登记名称，拒绝路径、空白或无效名称，返回字符串。"""
        name = next((item.get("quality_dir") for item in self.data["files"] if item.get("quality_id") == quality.code and item.get("quality_dir")), quality.directory)
        if Path(name).name != name or Path(name).drive or re.search(r'\s', name) or name in ('.', '..'):
            raise ToolError(f"清晰度目录无效：{name}")
        return name

    def quality_directory(self, quality: Quality, *, role: str = 'part') -> Path:
        """返回 quality 的同级用途目录；role 为 part/merged，优先复用已登记分P目录名称，返回 Path。"""
        if role not in ('part', 'merged'):
            raise ToolError(f'目录用途无效：{role}')
        base = self.base_directory
        result = naming.role_folder(base, self.quality_suffix(quality), self.mix_quality, role)
        if not self.mix_quality and role == 'part' and quality.code in self._quality_folders:
            result = self._quality_folders[quality.code]
        if result.is_symlink() or result.is_junction() or not result.resolve().is_relative_to(self.output_dir):
            raise ToolError(f"清晰度目录不能是链接或越界：{result}")
        return result

    def organize_qualities(self, *, mix_quality: bool | None = None) -> None:
        """按清晰度和用途整理媒体及索引；mix_quality 为目标布局，None 保留磁盘布局，失败回滚，无返回值。"""
        from .layout import organize_submission

        organize_submission(self, mix_quality=mix_quality)

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

    def flatten_parts(self) -> None:
        """整理本投稿旧分 P 目录及单 P 断点流；冲突不覆盖，移动或保存失败回滚，返回 None。"""
        root = self.base_directory if self.base_directory.is_dir() else self.path.parent
        bvid = self.data["bvid"]
        pattern = re.compile(re.escape(bvid) + r"_p[1-9]\d*")
        sources = {path for path in root.iterdir() if pattern.fullmatch(path.name)} if root.is_dir() else set()
        for item in self.data["files"]:
            if item.get("path"):
                parent = self.resolve_file(item).parent
                if pattern.fullmatch(parent.name) and parent.parent == self.output_dir:
                    sources.add(parent)
        moves: list[tuple[Path, Path]] = []
        for source in sorted(sources):
            if source.is_symlink() or source.is_junction() or not source.is_dir() or source.resolve() != source:
                raise ToolError(f"分 P 迁移目录无效：{source}")
            for child in sorted(source.iterdir()):
                destination = root / ".streams" / source.name if child.name == ".streams" else root / child.name
                moves.append((child, destination))
        streams = root / ".streams"
        if streams.exists() or streams.is_symlink() or streams.is_junction():
            if streams.is_symlink() or streams.is_junction() or not streams.is_dir():
                raise ToolError(f"断点流目录无效：{streams}")
            children = list(streams.iterdir())
            if any(path.is_symlink() or path.is_junction() for path in children):
                raise ToolError(f"断点流目录包含链接，无法直接迁移：{streams}")
            stream_pattern = re.compile(r"BV[0-9A-Za-z]{10}_p[1-9]\d*")
            old_streams = [path for path in children if not (path.is_dir() and stream_pattern.fullmatch(path.name))]
            if old_streams:
                parts = {item.get("part") for item in self.data["files"] if item.get("kind") == "part"}
                if self.data.get("total_parts") != 1 or parts - {1}:
                    raise ToolError(f"旧断点流无法确定分 P，请检查：{streams}")
                moves.extend((path, streams / f"{bvid}_p1" / path.name) for path in sorted(old_streams))
        targets: set[str] = set()
        for old, new in moves:
            key = new.as_posix().casefold()
            if any(key == target or key.startswith(target + "/") or target.startswith(key + "/") for target in targets) or new.exists() or new.is_symlink():
                raise ToolError(f"分 P 目录迁移存在同名目标，不覆盖：{new}")
            targets.add(key)
            if old.is_symlink() or old.is_junction() or (old.is_dir() and any(p.is_symlink() or p.is_junction() for p in old.rglob("*"))):
                raise ToolError(f"分 P 目录包含链接，无法直接迁移：{old}")
        replacements = []
        for item in self.data["files"]:
            if item.get("path"):
                path = self.resolve_file(item)
                for old, new in moves:
                    if path == old or path.is_relative_to(old):
                        replacements.append((item, (new / path.relative_to(old)).relative_to(self.path.parent, walk_up=True).as_posix()))
                        break
        original = deepcopy(self.data)
        moved: list[tuple[Path, Path]] = []
        created: list[Path] = []
        try:
            for old, new in moves:
                missing = []
                parent = new.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for directory in reversed(missing):
                    directory.mkdir()
                    created.append(directory)
                old.rename(new)
                moved.append((old, new))
            for item, relative in replacements:
                item["path"] = relative
            if moves:
                self.save()
        except BaseException as exc:
            self.data = original
            for old, new in reversed(moved):
                try:
                    new.rename(old)
                except OSError as rollback_error:
                    exc.add_note(f"目录迁移回滚失败：{new} -> {old}：{rollback_error}")
            for directory in reversed(created):
                try:
                    directory.rmdir()
                except OSError as rollback_error:
                    exc.add_note(f"迁移目录清理失败：{directory}：{rollback_error}")
            raise
        for source in sorted(sources):
            if source.exists():
                source.rmdir()

    def refresh_local(self, executable: str) -> None:
        """用 executable 更新已登记文件并扫描同一基础目录的各清晰度文件夹；排除临时文件，无返回值。"""
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
        base = self.base_directory
        roots = {base} if base.is_dir() else set()
        for folder in base.parent.glob(base.name + '_*'):
            if folder == base.with_name(base.name + '_分P') and folder.is_dir():
                if folder.is_symlink() or folder.is_junction():
                    raise ToolError(f'分 P 目录不能是链接：{folder}')
                roots.add(folder)
                continue
            suffix = folder.name[len(base.name) + 1:]
            if suffix.endswith('_合并'):
                continue
            suffix = suffix.removesuffix('_分P')
            if re.fullmatch(r'qn[1-9]\d*_.+', suffix) or suffix == LEGACY_QUALITY.directory:
                if folder.is_symlink() or folder.is_junction():
                    raise ToolError(f'清晰度目录不能是链接：{folder}')
                if folder.is_dir():
                    roots.add(folder)
        paths = {path for folder in roots for path in folder.rglob('*')}
        for path in sorted(paths):
            relative = path.relative_to(self.path.parent, walk_up=True)
            if not path.is_file() or path.suffix.lower() not in (".mp4", ".mkv") or path.resolve() in known:
                continue
            if any(piece.startswith(".") and piece != ".." for piece in relative.parts) or ".partial." in path.name:
                continue
            if self.path.name != "video_info.json" and self.data["bvid"] not in path.stem:
                continue
            match = re.fullmatch(re.escape(self.data["bvid"]) + r"_p(\d+)", path.parent.name)
            if not match:
                match = re.fullmatch(r".*_" + re.escape(self.data["bvid"]) + r"_p(\d+)(?:_(qn([1-9]\d*)_(.+)))?", path.stem)
            part = int(match[1]) if match else None
            entry_id = f"{self.data['bvid']}_p{part}" if part else f"video:{relative.as_posix()}"
            kind = "part" if part else "video"
            suffix = path.parent.name.removeprefix(base.name + '_').removesuffix('_分P')
            directory_match = re.fullmatch(r'qn([1-9]\d*)_(.+)', suffix)
            inferred = Quality(int(directory_match[1]), directory_match[2]) if directory_match else LEGACY_QUALITY
            quality = next((entry_quality(item) for item in self.data["files"] if item.get("quality_dir") == suffix), inferred)
            tag = None
            if self.mix_quality:
                named = re.fullmatch(r'.*_' + re.escape(self.data['bvid']) + r'_p[1-9]\d*_(qn([1-9]\d*)_(.+))', path.stem)
                if named:
                    tag = named[1]
                    quality = next((entry_quality(item) for item in self.data['files'] if item.get('quality_dir') == tag),
                                   Quality(int(named[2]), named[3].replace('_', ' ')))
            if any(item["id"] == quality_entry_id(entry_id, quality) for item in self.data["files"]):
                # 同分 P 的其他容器/历史文件只登记为附加视频，不能覆盖已确认的格式签名与路径。
                entry_id, kind = f"video:{relative.as_posix()}", "video"
            item = self.entry(entry_id, kind, part, path.stem, quality=quality)
            if self.mix_quality and tag:
                item.update(base_stem=path.stem[:-len(tag) - 1], mix_quality=True, stem=path.stem, quality_dir=tag)
            # 未经下载记录确认的旧文件不推断 format_id，不能据此跳过新选择的格式。
            self.record_file(item, path)
            self.enrich_file(item, executable)
            if item["video_codec"] is None:
                item["status"] = "failed"
        self.organize_qualities()
        self.save()


def remove_empty_directories(root: Path, output_dir: Path) -> None:
    """清理 output_dir 内 root 下的空目录；跳过链接和非空目录，无返回值，不删除下载根目录。"""
    if root.is_symlink() or root.is_junction():
        return
    root = root.resolve()
    if root == output_dir.resolve() or not root.is_relative_to(output_dir.resolve()) or not root.is_dir():
        return
    directories = [path for path in root.rglob('*') if path.is_dir() and not path.is_symlink() and not path.is_junction()]
    for directory in sorted([root, *directories], key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            # 仅清理空目录；保留其他投稿尚在使用的目录和未识别文件。
            pass




def find_video_folders(output_dir: Path, bvid: str) -> list[Path]:
    """在 output_dir 内查找 bvid 的逻辑投稿目录组；同组不同画质去重，返回每组一个代表目录。"""
    from .source_records import index_paths

    groups = {}
    for path in index_paths(output_dir, bvid):
        info = VideoInfo(output_dir, path.parent, bvid, '', shared=path.name != 'video_info.json')
        groups.setdefault(info.base_directory, path.parent)
    for folder in output_dir.rglob(f'*_{bvid}'):
        if folder.is_dir() and not folder.is_symlink() and not folder.is_junction():
            groups.setdefault(folder.resolve(), folder.resolve())
    return sorted(groups.values())


def move_to_collection(info: VideoInfo, folder: Path) -> VideoInfo:
    """将 info 的分P和合并结果迁入 folder 对应目录；保留独立合并索引，失败回滚，返回 info。"""
    from .layout import organize_submission

    if info.path.name != "video_info.json" and info.base_directory.resolve() != folder.resolve():
        raise ToolError(f"{info.data['bvid']} 已在其他合集目录：{info.base_directory}")
    organize_submission(info, target_base=folder, shared=True)
    return info


def open_video(output_dir: Path, bvid: str, title: str | None = None, total_parts: int | None = None, *, shared_folder: Path | None = None) -> VideoInfo:
    """在 output_dir 聚合 bvid 同一目录组的各画质索引；title/total_parts 用于新建，shared_folder 指定合集，返回 VideoInfo。"""
    from .source_records import index_paths, load_views

    folders = find_video_folders(output_dir, bvid)
    paths = index_paths(output_dir, bvid)
    if len(folders) > 1:
        raise ToolError(f"{bvid} 存在多个投稿目录，请整理后重试：{folders}")
    if paths:
        if len({path.name for path in paths}) != 1:
            raise ToolError(f'{bvid} 的普通和合集源索引混放，请整理后重试')
        path = paths[0]
        info = VideoInfo(output_dir, path.parent, bvid, title or read_json(path).get('title', ''), total_parts,
                         shared=path.name != 'video_info.json')
        load_views(info, paths)
        if shared_folder is not None and info.base_directory.resolve() != shared_folder.resolve():
            raise ToolError(f"{bvid} 已在其他合集目录：{info.base_directory}")
        return info
    if folders:
        folder = folders[0]
        title = title or (read_json(folder / 'video_info.json').get('title', '') if (folder / 'video_info.json').is_file() else folder.name[:-(len(bvid) + 1)])
    else:
        if not title:
            raise ToolError(f"未找到 {bvid} 的投稿目录")
        # title 由下载入口沿用现有 safe_name 规则清理，迁移入口也必须先清理。
        folder = shared_folder if shared_folder is not None else output_dir / f"{title}_{bvid}"
    return VideoInfo(output_dir, folder, bvid, title, total_parts, shared=not folders and shared_folder is not None)


def discover_videos(output_dir: Path) -> tuple[list[str], list[str]]:
    """发现 output_dir 及分组目录内的投稿与旧下载记录；返回 BV 号列表及损坏旧记录的错误列表。"""
    ids: set[str] = set()
    errors: list[str] = []
    for folder in output_dir.rglob("*") if output_dir.is_dir() else []:
        match = re.search(r"_(BV[0-9A-Za-z]{10})$", folder.name)
        if folder.is_dir() and not folder.is_symlink() and not folder.is_junction() and match:
            ids.add(match[1])
        shared = re.fullmatch(r"(BV[0-9A-Za-z]{10})_video_info\.json", folder.name)
        if folder.is_file() and not folder.is_symlink() and shared:
            ids.add(shared[1])
        if folder.name == 'video_info.json' and folder.is_file() and not folder.is_symlink():
            try:
                data = read_json(folder)
                if data.get('record_type') == 'merged':
                    continue
                bvid = data.get('bvid', '')
                if re.fullmatch(r'BV[0-9A-Za-z]{10}', bvid):
                    ids.add(bvid)
            except ToolError as exc:
                errors.append(str(exc))
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
        migrated.append((path, item))
    if not migrated:
        return
    info.data["total_parts"] = next(iter(totals)) if totals else None
    if info.data["total_parts"] is not None:
        info.data["errors"] = [error for error in info.data["errors"] if error != "尚未获取总分 P 数"]
    # download.json 仍指向旧分 P 子目录；先按旧格式持久化并核对，随后由布局迁移升级为目录索引。
    info.data['schema_version'] = 4
    info.save()
    verified = read_json(info.path)
    for path, item in migrated:
        if item not in verified["files"]:
            raise ToolError(f"旧记录迁移核对失败，保留原记录：{path}")
    for path, _ in migrated:
        path.unlink()


