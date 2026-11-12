"""离线验证统一视频记录、旧记录迁移及下载生命周期；不使用真实账号或原站接口。"""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import download_bilibili as tool
import video_metadata as metadata
from tests import PROJECT_DIR


class VideoMetadataTests(unittest.TestCase):
    """使用临时媒体和模拟探测结果验证唯一记录的持久化与失败恢复。"""

    def setUp(self) -> None:
        """创建临时下载目录、配置及选定格式；无返回值。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = replace(tool.load_settings(PROJECT_DIR / "config.yaml"), output_dir=self.root)
        self.bvid = "BV1xx411c7mD"
        self.title = "测试投稿"
        self.fmt = {"format_id": "30064", "vcodec": "avc1", "acodec": "none", "width": 64, "height": 64, "fps": 5}
        self.probe = {"duration_seconds": 1.0, "width": 64, "height": 64, "fps": 5.0,
                      "video_codec": "h264", "audio_codec": None, "errors": ["本地文件未提供 audio_codec"]}

    def tearDown(self) -> None:
        """清理临时下载目录；无返回值。"""
        self.temporary.cleanup()

    def legacy(self, part: int = 1, folder: str | None = None, **updates: object) -> Path:
        """创建 part 的旧记录；folder 可覆盖目录名，updates 覆盖旧字段，返回 download.json 路径。"""
        destination = self.root / (folder or f"{self.title}_{self.bvid}/{self.bvid}_p{part}")
        destination.mkdir(parents=True, exist_ok=True)
        filename = "video.mp4" if part == 1 else f"video{part}.mp4"
        (destination / filename).write_bytes(b"local-video")
        record = {"id": f"{self.bvid}_p{part}", "title": f"片段{part}", "video_title": self.title,
                  "total_parts": 2, "file": filename, "stem": Path(filename).stem, "formats": [self.fmt], "processing": "stream copy"}
        record.update(updates)
        path = destination / "download.json"
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return path

    def download(self, part: int, interrupt: BaseException | None = None, total_parts: int = 2) -> MagicMock:
        """模拟下载 part，interrupt 可中断传输，total_parts 为投稿总分 P 数；检查开始状态并写流，返回下载器。"""
        downloader = MagicMock()

        def transfer(filename: str, info: dict) -> tuple[bool, bool]:
            """检查 filename/info 对应的下载中状态，写入流并按需中断，成功返回传输结果。"""
            stored = tool.get_video_info(self.settings, self.bvid)
            entry = next(item for item in stored.data["files"] if item["id"] == info["id"])
            self.assertEqual(entry["status"], "downloading")
            Path(filename).write_bytes(b"stream")
            if interrupt:
                raise interrupt
            return True, True

        def mux(executable: str, sources: list[Path], formats: list[dict], folder: Path, stem: str) -> Path:
            """模拟 executable 将 sources/formats 封装到 folder/stem；返回正式媒体路径。"""
            path = folder / f"{stem}.mp4"
            path.write_bytes(sources[0].read_bytes())
            return path

        downloader.dl.side_effect = transfer
        info = {"id": f"{self.bvid}_p{part}", "title": f"片段{part}",
                "requested_formats": [{**self.fmt, "url": "https://example.com/file", "ext": "mp4"}]}
        with patch.object(tool, "mux_streams", side_effect=mux), patch.object(metadata, "probe_media", return_value=self.probe):
            tool.save_video(downloader, info, self.settings, "ffmpeg", self.title, total_parts)
        return downloader

    def test_parts_share_one_record_and_repeat_download_skips(self) -> None:
        """验证分次下载汇总、开始状态和完成跳过，且不会生成分 P 记录文件；无返回值。"""
        self.download(2)
        self.download(1)
        downloader = self.download(1)
        downloader.dl.assert_not_called()
        record = tool.get_video_info(self.settings, self.bvid)
        self.assertEqual(len(record.data["files"]), 2)
        self.assertTrue(all(item["status"] == "completed" for item in record.data["files"]))
        self.assertEqual(len(list(self.root.rglob("video_info.json"))), 1)
        self.assertEqual(list(self.root.rglob("download.json")), [])
        self.assertEqual(record.data["source_url"], f"https://www.bilibili.com/video/{self.bvid}")
        self.assertEqual(record.data["files"][0]["source_url"], f"https://www.bilibili.com/video/{self.bvid}?p=2")
        self.assertTrue(all(record.resolve_file(item).parent == record.path.parent for item in record.data["files"]))

    def test_grouped_post_remains_discoverable_and_skips_repeat_download(self) -> None:
        """验证投稿移入分组后仍可查找、刷新和跳过重复下载；无返回值。"""
        self.download(1, total_parts=1)
        record = tool.get_video_info(self.settings, self.bvid)
        group = self.root / "合集"
        group.mkdir()
        destination = group / record.path.parent.name
        record.path.parent.rename(destination)
        grouped = tool.get_video_info(self.settings, self.bvid, self.title, 1)
        self.assertEqual(grouped.path.parent, destination.resolve())
        self.assertEqual(metadata.discover_videos(self.root), ([self.bvid], []))
        self.download(1, total_parts=1).dl.assert_not_called()
        self.assertEqual(len(list(self.root.rglob("video_info.json"))), 1)

    def test_duplicate_post_between_root_and_group_is_rejected(self) -> None:
        """验证根目录与分组出现相同 BV 号时明确报错，不覆盖或重复下载；无返回值。"""
        self.download(1, total_parts=1)
        duplicate = self.root / "合集" / f"另一个标题_{self.bvid}"
        duplicate.mkdir(parents=True)
        with self.assertRaises(tool.ToolError):
            tool.get_video_info(self.settings, self.bvid, self.title, 1)

    def test_incremental_parts_save_subtitles_together(self) -> None:
        """验证分次下载的字幕使用各自分 P 前缀并与视频同目录；无返回值。"""
        context = MagicMock()
        response = context.request.get.return_value
        response.ok = True
        response.json.return_value = {"body": [{"from": 0, "to": 1, "content": "字幕"}]}
        track = tool.SubtitleTrack("zh", "中文", False, "https://example.com/subtitle", "1")
        for part in (2, 1):
            self.download(part)
            record = tool.get_video_info(self.settings, self.bvid)
            item = next(item for item in record.data["files"] if item["part"] == part)
            self.assertEqual(tool.fetch_subtitles(context, [track], record.path.parent, item["stem"], self.settings), [])
            self.assertTrue((record.path.parent / f"{item['stem']}.zh.srt").is_file())
        self.assertEqual(len(list(record.path.parent.glob("*.zh.srt"))), 2)

    def test_single_part_download_uses_post_directory_and_repeat_skips(self) -> None:
        """验证单分 P 新下载直接落在投稿目录，重复运行无需重新下载；无返回值。"""
        self.download(1, total_parts=1)
        record = tool.get_video_info(self.settings, self.bvid)
        item = record.data["files"][0]
        self.assertEqual(record.resolve_file(item).parent, record.path.parent)
        self.assertNotIn("/", item["path"])
        self.assertFalse((record.path.parent / f"{self.bvid}_p1").exists())
        self.assertEqual(item["id"], f"{self.bvid}_p1")
        self.assertEqual(item["formats"], [self.fmt])
        self.download(1, total_parts=1).dl.assert_not_called()

    def test_one_selected_part_of_multi_part_post_uses_post_directory(self) -> None:
        """验证多分 P 投稿只选择 P1 时也直接保存到投稿目录；无返回值。"""
        self.download(1, total_parts=5)
        record = tool.get_video_info(self.settings, self.bvid)
        self.assertEqual(record.resolve_file(record.data["files"][0]).parent, record.path.parent)
        self.assertEqual(record.data["total_parts"], 5)

    def test_multiple_part_directories_migrate_with_independent_streams(self) -> None:
        """验证多个旧分 P 的视频、字幕与同名断点流迁移后互不覆盖；无返回值。"""
        first, second = self.legacy(1), self.legacy(2)
        for number, old in enumerate((first, second), 1):
            (old.parent / f"subtitle{number}.srt").write_text(f"字幕{number}", encoding="utf-8")
            (old.parent / ".streams").mkdir()
            (old.parent / ".streams/0.raw.part").write_bytes(f"stream{number}".encode())
        record = tool.get_video_info(self.settings, self.bvid)
        for number, old in enumerate((first, second), 1):
            self.assertFalse(old.parent.exists())
            self.assertEqual((record.path.parent / f"subtitle{number}.srt").read_text(encoding="utf-8"), f"字幕{number}")
            self.assertEqual((record.path.parent / ".streams" / f"{self.bvid}_p{number}" / "0.raw.part").read_bytes(), f"stream{number}".encode())
        self.assertTrue(all(record.resolve_file(item).parent == record.path.parent for item in record.data["files"]))

    def test_multi_part_migration_preflights_conflicts(self) -> None:
        """验证多个旧目录的目标冲突在移动前报错，所有原文件保留；无返回值。"""
        first, second = self.legacy(1), self.legacy(2)
        (second.parent / "video.mp4").write_bytes(b"different")
        record = metadata.open_video(self.root, self.bvid, self.title, 2)
        metadata.migrate_legacy(record)
        original = record.path.read_bytes()
        with self.assertRaises(tool.ToolError):
            record.flatten_parts()
        self.assertEqual((first.parent / "video.mp4").read_bytes(), b"local-video")
        self.assertEqual((second.parent / "video.mp4").read_bytes(), b"different")
        self.assertFalse((record.path.parent / "video.mp4").exists())
        self.assertEqual(record.path.read_bytes(), original)

    def test_old_single_part_flat_streams_migrate(self) -> None:
        """验证投稿目录下旧单 P 断点流在总数更新前被归入 P1；无返回值。"""
        self.download(1, total_parts=1)
        record = tool.get_video_info(self.settings, self.bvid)
        streams = record.path.parent / ".streams"
        streams.mkdir()
        (streams / "0.raw.part").write_bytes(b"partial")
        tool.get_video_info(self.settings, self.bvid, self.title, 2)
        self.assertEqual((streams / f"{self.bvid}_p1/0.raw.part").read_bytes(), b"partial")

    def test_incomplete_parts_use_separate_streams(self) -> None:
        """验证两个中断的分 P 分别保存同名流并能够独立续传；无返回值。"""
        for part in (1, 2):
            with self.assertRaises(KeyboardInterrupt):
                self.download(part, KeyboardInterrupt())
        record = tool.get_video_info(self.settings, self.bvid)
        for part in (1, 2):
            self.assertTrue((record.path.parent / ".streams" / f"{self.bvid}_p{part}" / "0.30064.mp4").is_file())
            self.download(part)
        self.assertTrue(all(item["status"] == "completed" for item in tool.get_video_info(self.settings, self.bvid).data["files"]))

    def test_single_part_migration_moves_subtitles_and_partial_streams(self) -> None:
        """验证旧单 P 目录连同字幕及断点流迁移，记录更新且文件属性、格式签名不变；无返回值。"""
        old = self.legacy(total_parts=1)
        source = old.parent / "video.mp4"
        before = source.stat()
        (old.parent / "video.ai-zh.ai.srt").write_text("字幕", encoding="utf-8")
        (old.parent / ".streams").mkdir()
        (old.parent / ".streams/0.raw.mp4.part").write_bytes(b"partial")
        record = tool.get_video_info(self.settings, self.bvid)
        item = record.data["files"][0]
        destination = record.resolve_file(item)
        self.assertEqual(destination, record.path.parent / "video.mp4")
        self.assertEqual((destination.stat().st_size, destination.stat().st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.assertEqual((record.path.parent / "video.ai-zh.ai.srt").read_text(encoding="utf-8"), "字幕")
        self.assertEqual((record.path.parent / ".streams" / f"{self.bvid}_p1/0.raw.mp4.part").read_bytes(), b"partial")
        self.assertEqual(item["formats"], [self.fmt])
        self.assertFalse(old.parent.exists())
        self.assertEqual(list(self.root.rglob("download.json")), [])
        self.assertEqual(len(list(self.root.rglob("video_info.json"))), 1)
        self.assertEqual(tool.get_video_info(self.settings, self.bvid).data["files"], record.data["files"])
        self.download(1, total_parts=1).dl.assert_not_called()

    def test_single_part_migration_rolls_back_on_record_write_failure(self) -> None:
        """验证目录搬移后的记录写入失败会还原文件和内存数据，磁盘原记录不变；无返回值。"""
        old = self.legacy(total_parts=1)
        record = metadata.open_video(self.root, self.bvid, self.title, 1)
        metadata.migrate_legacy(record)
        original = record.path.read_bytes()
        old_path = record.data["files"][0]["path"]
        with patch.object(record, "save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            record.flatten_parts()
        self.assertTrue((old.parent / "video.mp4").exists())
        self.assertFalse((record.path.parent / "video.mp4").exists())
        self.assertEqual(record.path.read_bytes(), original)
        self.assertEqual(record.data["files"][0]["path"], old_path)

    def test_single_part_migration_refuses_existing_destination(self) -> None:
        """验证同名目标冲突时不覆盖任何文件，且不修改信息记录；无返回值。"""
        old = self.legacy(total_parts=1)
        record = metadata.open_video(self.root, self.bvid, self.title, 1)
        metadata.migrate_legacy(record)
        original = record.path.read_bytes()
        destination = record.path.parent / "video.mp4"
        destination.write_bytes(b"other-video")
        with self.assertRaises(tool.ToolError):
            record.flatten_parts()
        self.assertEqual(destination.read_bytes(), b"other-video")
        self.assertEqual((old.parent / "video.mp4").read_bytes(), b"local-video")
        self.assertEqual(record.path.read_bytes(), original)

    def test_single_part_migration_rolls_back_partial_moves(self) -> None:
        """验证部分文件搬移失败时还原已搬文件，保留原目录及记录；无返回值。"""
        old = self.legacy(total_parts=1)
        (old.parent / "video.srt").write_bytes(b"subtitle")
        record = metadata.open_video(self.root, self.bvid, self.title, 1)
        metadata.migrate_legacy(record)
        original_data = record.path.read_bytes()
        original_rename = Path.rename

        def rename(path: Path, target: Path) -> Path:
            """将 path 移至 target，仅在搬移字幕时模拟错误，其他搬移或回滚正常执行；返回目标路径。"""
            if path.name == "video.srt" and path.parent.name == f"{self.bvid}_p1":
                raise PermissionError("locked")
            return original_rename(path, target)

        with patch.object(Path, "rename", new=rename), self.assertRaises(PermissionError):
            record.flatten_parts()
        self.assertEqual((old.parent / "video.mp4").read_bytes(), b"local-video")
        self.assertEqual((old.parent / "video.srt").read_bytes(), b"subtitle")
        self.assertFalse((record.path.parent / "video.mp4").exists())
        self.assertEqual(record.path.read_bytes(), original_data)

    def test_refresh_flattens_even_when_total_part_count_is_unknown(self) -> None:
        """验证旧记录总数未知时也整理分 P 目录，并可继续刷新原站信息；无返回值。"""
        old = self.legacy(total_parts=None)
        record = tool.get_video_info(self.settings, self.bvid)
        self.assertFalse(old.parent.exists())
        data = {"bvid": self.bvid, "title": self.title, "owner": {"mid": 1, "name": "test"}, "videos": 1}
        with patch.object(metadata, "probe_media", return_value={**self.probe, "errors": []}), patch.object(tool, "authenticated_session", return_value=MagicMock()), patch.object(tool, "ensure_login"), patch.object(tool, "api_json", return_value={"data": data}):
            self.assertEqual(tool.refresh_metadata(self.settings, tool.parse_args(["--refresh-metadata"]), "ffmpeg"), 0)
        record = tool.get_video_info(self.settings, self.bvid)
        self.assertFalse(old.parent.exists())
        self.assertEqual(record.data["files"][0]["path"], "video.mp4")

    def test_interrupt_preserves_other_parts_and_marks_current(self) -> None:
        """验证中断当前分 P 时保留既有完成记录及下载流，重试后恢复完成；无返回值。"""
        self.download(1)
        with self.assertRaises(KeyboardInterrupt):
            self.download(2, KeyboardInterrupt())
        record = tool.get_video_info(self.settings, self.bvid)
        states = {item["part"]: item["status"] for item in record.data["files"]}
        self.assertEqual(states, {1: "completed", 2: "interrupted"})
        self.assertTrue(list(self.root.rglob(".streams/*")))
        self.download(2)
        self.assertTrue(all(item["status"] == "completed" for item in tool.get_video_info(self.settings, self.bvid).data["files"]))

    def test_migration_preserves_signature_and_skip_without_download(self) -> None:
        """验证迁移后保留精确格式签名和字幕前缀，可跳过下载且不修改媒体文件；无返回值。"""
        old = self.legacy()
        source = old.parent / "video.mp4"
        before = source.stat()
        with patch.object(metadata, "probe_media", return_value=self.probe):
            downloader = self.download(1)
        downloader.dl.assert_not_called()
        record = tool.get_video_info(self.settings, self.bvid)
        self.assertEqual(record.data["files"][0]["formats"], [self.fmt])
        self.assertEqual(record.data["files"][0]["stem"], "video")
        source = record.resolve_file(record.data["files"][0])
        self.assertEqual((source.stat().st_size, source.stat().st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.assertFalse(old.exists())
        tool.get_video_info(self.settings, self.bvid)
        self.assertEqual(len(record.data["files"]), 1)

    def test_migration_write_failure_keeps_old_records(self) -> None:
        """验证原子写入失败时旧记录不删除、临时文件不遗留；无返回值。"""
        old = self.legacy()
        with patch.object(metadata, "atomic_text", side_effect=OSError("disk full")), self.assertRaises(OSError):
            tool.get_video_info(self.settings, self.bvid)
        self.assertTrue(old.exists())
        self.assertEqual(list(self.root.rglob("video_info.json")), [])

    def test_migration_verification_failure_keeps_old(self) -> None:
        """验证保存后的核对不通过时保留旧文件；无返回值。"""
        old = self.legacy()
        original = metadata.read_json

        def read(path: Path) -> dict:
            """读取 path，针对新信息文件模拟核对内容缺失；返回 JSON 字典。"""
            result = original(path)
            if path.name == "video_info.json":
                result["files"] = []
            return result

        with patch.object(metadata, "read_json", side_effect=read), self.assertRaises(tool.ToolError):
            tool.get_video_info(self.settings, self.bvid)
        self.assertTrue(old.exists())

    def test_conflicting_old_records_are_not_removed(self) -> None:
        """验证同分 P 不同来源记录冲突时均保留，且不保存不完整迁移；无返回值。"""
        first = self.legacy()
        second = self.legacy(folder="another")
        with self.assertRaises(tool.ToolError):
            tool.get_video_info(self.settings, self.bvid)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(list(self.root.rglob("video_info.json")), [])

    def test_migration_retries_after_partial_removal(self) -> None:
        """验证部分旧文件删除失败后重复迁移不会丢失或重复记录；无返回值。"""
        self.legacy(1)
        second = self.legacy(2)
        original = Path.unlink

        def remove(path: Path, missing_ok: bool = False) -> None:
            """按 missing_ok 删除 path，仅对第二个旧记录模拟权限错误；无返回值。"""
            if path.resolve() == second.resolve():
                raise PermissionError("locked")
            original(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", new=remove), self.assertRaises(PermissionError):
            tool.get_video_info(self.settings, self.bvid)
        self.assertTrue(second.exists())
        record = tool.get_video_info(self.settings, self.bvid)
        self.assertEqual(len(record.data["files"]), 2)
        self.assertEqual(list(self.root.rglob("download.json")), [])

    def test_corrupt_and_inconsistent_legacy_records_are_kept(self) -> None:
        """验证损坏记录或总数冲突明确报错，不删除原始记录；无返回值。"""
        first = self.legacy()
        second = self.legacy(2, total_parts=3)
        with self.assertRaises(tool.ToolError):
            tool.get_video_info(self.settings, self.bvid)
        self.assertTrue(first.exists())
        second.write_text("{broken", encoding="utf-8")
        with self.assertRaises(tool.ToolError):
            tool.get_video_info(self.settings, self.bvid)
        self.assertTrue(second.exists())

    def test_completion_write_failure_keeps_downloaded_streams(self) -> None:
        """验证完成状态未保存时不删除下载流，磁盘上不会虚报完成；无返回值。"""
        original = metadata.VideoInfo.save

        def save(record: metadata.VideoInfo) -> None:
            """保存 record，但模拟完成状态的磁盘写入失败；无返回值。"""
            if any(item["status"] == "completed" for item in record.data["files"]):
                raise OSError("disk full")
            original(record)

        with patch.object(metadata.VideoInfo, "save", new=save), self.assertRaises(OSError):
            self.download(1)
        record = tool.get_video_info(self.settings, self.bvid)
        self.assertEqual(record.data["files"][0]["status"], "downloading")
        self.assertTrue(list(self.root.rglob(".streams/*")))

    def test_probe_failure_keeps_confirmed_completion(self) -> None:
        """验证文件头探测失败只记录原因，不改变已经保存的完成状态；无返回值。"""
        old = self.legacy()
        record = tool.get_video_info(self.settings, self.bvid)
        with patch.object(metadata, "probe_media", side_effect=tool.ToolError("probe failed")):
            record.enrich_file(record.data["files"][0], "ffmpeg")
            record.save()
        item = metadata.read_json(record.path)["files"][0]
        self.assertEqual(item["status"], "completed")
        self.assertIn("probe failed", item["errors"])
        self.assertIsNone(item["duration_seconds"])
        self.assertFalse(old.exists())

    def test_refresh_discovers_standalone_media_and_ignores_temporary(self) -> None:
        """验证旧版独立视频可补生成记录，临时媒体不加入列表；无返回值。"""
        record = tool.get_video_info(self.settings, self.bvid, self.title)
        record.path.parent.mkdir(parents=True)
        source = record.path.parent / "完整视频.mp4"
        source.write_bytes(b"video")
        (record.path.parent / "video.partial.mp4").write_bytes(b"partial")
        (record.path.parent / ".streams").mkdir()
        (record.path.parent / ".streams/raw.mp4").write_bytes(b"stream")
        before = source.stat().st_mtime_ns
        with patch.object(metadata, "probe_media", return_value=self.probe):
            record.refresh_local("ffmpeg")
            record.refresh_local("ffmpeg")
        self.assertEqual(len(record.data["files"]), 1)
        item = record.data["files"][0]
        self.assertEqual((item["kind"], item["size_bytes"], item["path"]), ("video", 5, source.name))
        self.assertEqual(source.stat().st_mtime_ns, before)

    def test_refresh_discovers_flat_part_by_filename(self) -> None:
        """验证投稿目录中无记录的分 P 按文件名恢复编号，临时流不会被登记；无返回值。"""
        record = tool.get_video_info(self.settings, self.bvid, self.title)
        record.path.parent.mkdir(parents=True)
        source = record.path.parent / f"片段2_{self.bvid}_p2.mp4"
        source.write_bytes(b"video")
        with patch.object(metadata, "probe_media", return_value=self.probe):
            record.refresh_local("ffmpeg")
            record.refresh_local("ffmpeg")
        self.assertEqual(len(record.data["files"]), 1)
        self.assertEqual((record.data["files"][0]["kind"], record.data["files"][0]["part"]), ("part", 2))

    def test_refresh_auth_failure_still_migrates_local_info(self) -> None:
        """验证认证失败时仍完成本地迁移并记录缺失原站信息，不触发下载或拼接；无返回值。"""
        old = self.legacy()
        args = tool.parse_args(["--refresh-metadata"])
        with patch.object(metadata, "probe_media", return_value={**self.probe, "errors": []}), patch.object(tool, "authenticated_session", side_effect=tool.ToolError("expired")), patch.object(tool, "make_downloader") as download, patch.object(tool, "merge_parts") as merge:
            self.assertEqual(tool.refresh_metadata(self.settings, args, "ffmpeg"), 2)
        record = tool.get_video_info(self.settings, self.bvid)
        self.assertIsNone(record.data["uploader"])
        self.assertIn("expired", record.data["errors"][0])
        self.assertFalse(old.exists())
        download.assert_not_called()
        merge.assert_not_called()

    def test_refresh_remote_metadata_preserves_completion(self) -> None:
        """验证 UP 主信息经补生成更新，且不重置已有完成状态；无返回值。"""
        self.legacy()
        args = tool.parse_args(["--refresh-metadata"])
        session = MagicMock()
        data = {"bvid": self.bvid, "title": self.title, "owner": {"mid": 123, "name": "测试 UP"}, "videos": 2}
        with patch.object(metadata, "probe_media", return_value={**self.probe, "errors": []}), patch.object(tool, "authenticated_session", return_value=session), patch.object(tool, "ensure_login"), patch.object(tool, "api_json", return_value={"data": data}):
            self.assertEqual(tool.refresh_metadata(self.settings, args, "ffmpeg"), 0)
        record = tool.get_video_info(self.settings, self.bvid)
        self.assertEqual(record.data["uploader"], {"id": 123, "name": "测试 UP"})
        self.assertEqual(record.data["files"][0]["status"], "completed")

    def test_extra_media_does_not_replace_confirmed_part(self) -> None:
        """验证同分 P 的历史容器只作为附加视频登记，不覆盖续传使用的路径和签名；无返回值。"""
        self.legacy()
        record = tool.get_video_info(self.settings, self.bvid)
        original_path = record.data["files"][0]["path"]
        (record.path.parent / f"older_{self.bvid}_p1.mkv").write_bytes(b"old-container")
        with patch.object(metadata, "probe_media", return_value=self.probe):
            record.refresh_local("ffmpeg")
            record.refresh_local("ffmpeg")
        part = next(item for item in record.data["files"] if item["kind"] == "part")
        self.assertEqual((part["path"], part["formats"]), (original_path, [self.fmt]))
        self.assertEqual(len(record.data["files"]), 2)

    def test_extraction_failure_updates_pending_status(self) -> None:
        """验证传输前提取信息失败也将预登记分 P 标为失败；无返回值。"""
        args = tool.parse_args(["--url", self.bvid])
        target = tool.VideoTarget(f"https://www.bilibili.com/video/{self.bvid}?p=1", 123, 1, 2)
        session, downloader = MagicMock(), MagicMock()
        downloader.__enter__.return_value = downloader
        downloader.extract_info.side_effect = tool.ToolError("extract failed")
        source = {"bvid": self.bvid, "title": self.title, "owner": {"mid": 1, "name": "test"}, "videos": 2}
        with patch.object(tool, "load_settings", return_value=self.settings), patch.object(tool, "resolve_ffmpeg", return_value="ffmpeg"), patch.object(tool, "authenticated_session", return_value=session), patch.object(tool, "ensure_login", return_value={}), patch.object(tool, "resolve_targets", return_value=[target]), patch.object(tool, "api_json", return_value={"data": source}), patch.object(tool, "make_downloader", return_value=downloader):
            self.assertEqual(tool.run(args), 2)
        record = tool.get_video_info(self.settings, self.bvid)
        self.assertEqual(record.data["files"][0]["status"], "failed")
        self.assertIn("extract failed", record.data["files"][0]["errors"])

    def test_atomic_replace_failure_keeps_original_and_cleans_temporary(self) -> None:
        """验证原子替换失败不破坏已有内容，且清理临时写入文件；无返回值。"""
        record = tool.get_video_info(self.settings, self.bvid, self.title)
        record.save()
        original = record.path.read_bytes()
        record.data["title"] = "新标题"
        with patch.object(Path, "replace", side_effect=PermissionError("locked")), self.assertRaises(PermissionError):
            record.save()
        self.assertEqual(record.path.read_bytes(), original)
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_invalid_paths_and_metadata_fail_without_overwrite(self) -> None:
        """验证路径越界和损坏的新信息文件被拒绝，且不会被默认值覆盖；无返回值。"""
        record = tool.get_video_info(self.settings, self.bvid, self.title)
        for relative in ("../../outside.mp4", "C:/outside.mp4"):
            with self.subTest(relative=relative), self.assertRaises(tool.ToolError):
                record.resolve_file({"path": relative})
        record.save()
        record.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(tool.ToolError):
            tool.get_video_info(self.settings, self.bvid)
        self.assertEqual(record.path.read_text(encoding="utf-8"), "{broken")

    def test_refresh_mode_rejects_conflicting_arguments(self) -> None:
        """验证信息更新模式与下载目标、全部分 P 和其他模式互斥；无返回值。"""
        for arguments in (["--url", self.bvid], ["--all-parts"], ["--login-only"], ["--list-formats"], ["--merge-video", self.bvid]):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                tool.parse_args(["--refresh-metadata", *arguments])


if __name__ == "__main__":
    unittest.main()
