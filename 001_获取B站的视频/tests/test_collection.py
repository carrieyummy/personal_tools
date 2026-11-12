"""离线验证 B 站合集增量归档、旧投稿迁移及记录隔离。"""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import download_bilibili as tool
import video_metadata as metadata
from tests import PROJECT_DIR


class CollectionTests(unittest.TestCase):
    """用临时目录和模拟原站合集验证多投稿共享目录。"""

    def setUp(self) -> None:
        """创建临时下载目录、两个 BV 号及模拟合集信息；无返回值。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.settings = replace(tool.load_settings(PROJECT_DIR / "config.yaml"), output_dir=self.root)
        self.ids = ["BV1xx411c7mD", "BV1yy411c7mD"]
        self.season = {"bvid": self.ids[0], "ugc_season": {"id": 123, "title": "测试合集",
                       "sections": [{"episodes": [{"bvid": bvid} for bvid in self.ids]}]}}

    def tearDown(self) -> None:
        """删除临时下载目录；无返回值。"""
        self.temporary.cleanup()

    def old_post(self, bvid: str, *, grouped: bool = False) -> metadata.VideoInfo:
        """创建 bvid 的旧投稿、字幕及断点流；grouped 表示放入已有合集子目录，返回投稿记录。"""
        folder = self.root / ("测试合集_合集123" if grouped else "") / f"投稿_{bvid}"
        folder.mkdir(parents=True)
        record = metadata.VideoInfo(self.root, folder, bvid, "投稿", 1)
        video = folder / f"投稿_{bvid}_p1.mp4"
        video.write_bytes(b"video")
        (folder / f"投稿_{bvid}_p1.zh.srt").write_bytes(b"subtitle")
        streams = folder / ".streams" / f"{bvid}_p1"
        streams.mkdir(parents=True)
        (streams / "0.raw.part").write_bytes(b"partial")
        item = record.entry(f"{bvid}_p1", part=1)
        item["formats"] = [{"format_id": "raw"}]
        record.record_file(item, video)
        record.save()
        return record

    def test_incremental_collection_migrates_existing_members(self) -> None:
        """验证不同 BV 投稿的视频、字幕与断点流并排归档，记录仍可独立发现和刷新；无返回值。"""
        first = self.old_post(self.ids[0])
        second = self.old_post(self.ids[1], grouped=True)
        folder = tool.prepare_collection(self.settings, self.season)
        self.assertEqual(folder, self.root / "测试合集_合集123")
        self.assertFalse(first.path.parent.exists())
        self.assertFalse(second.path.parent.exists())
        for bvid in self.ids:
            record = tool.get_video_info(self.settings, bvid)
            self.assertEqual(record.path, folder / f"{bvid}_video_info.json")
            self.assertEqual(record.resolve_file(record.data["files"][0]), folder / f"投稿_{bvid}_p1.mp4")
            self.assertEqual((folder / f"投稿_{bvid}_p1.zh.srt").read_bytes(), b"subtitle")
            self.assertEqual((folder / ".streams" / f"{bvid}_p1/0.raw.part").read_bytes(), b"partial")
            with patch.object(metadata, "probe_media", return_value={"video_codec": "h264", "errors": []}):
                record.refresh_local("ffmpeg")
            self.assertEqual(len(record.data["files"]), 1)
        self.assertEqual(metadata.discover_videos(self.root)[0], sorted(self.ids))
        self.assertEqual(tool.prepare_collection(self.settings, self.season), folder)

    def test_new_member_uses_existing_collection_and_skips_repeat(self) -> None:
        """验证合集目录中新成员直接保存，重复下载根据独立记录跳过；无返回值。"""
        folder = self.root / "测试合集_合集123"
        folder.mkdir()
        self.assertEqual(tool.prepare_collection(self.settings, self.season), folder)
        record = tool.get_video_info(self.settings, self.ids[0], "投稿", 1, shared_folder=folder)
        record.save()
        fmt = {"format_id": "raw", "vcodec": "avc1", "acodec": "none", "url": "https://example.com/video", "ext": "mp4"}
        info = {"id": f"{self.ids[0]}_p1", "title": "投稿", "requested_formats": [fmt]}
        downloader = MagicMock()
        def transfer(filename: str, stream_info: dict) -> tuple[bool, bool]:
            """将模拟 stream_info 写入 filename，返回传输成功标记。"""
            Path(filename).write_bytes(b"stream")
            return True, True
        def mux(executable: str, sources: list[Path], formats: list[dict], target: Path, stem: str) -> Path:
            """模拟 executable 将 sources/formats 封装至 target/stem，返回视频路径。"""
            result = target / f"{stem}.mp4"
            result.write_bytes(sources[0].read_bytes())
            return result
        downloader.dl.side_effect = transfer
        with patch.object(tool, "mux_streams", side_effect=mux), patch.object(metadata, "probe_media", return_value={"errors": []}):
            self.assertEqual(tool.save_video(downloader, info, self.settings, "ffmpeg", "投稿", 1)[0], folder)
            downloader.dl.reset_mock()
            tool.save_video(downloader, info, self.settings, "ffmpeg", "投稿", 1)
            downloader.dl.assert_not_called()

    def test_run_downloads_different_members_into_one_collection(self) -> None:
        """验证两个独立下载入口使用原站合集信息并将不同 BV 保存到同一目录；无返回值。"""
        session = MagicMock()
        downloader = MagicMock()
        downloader.__enter__.return_value = downloader
        fmt = {"format_id": "raw", "vcodec": "avc1", "acodec": "none", "url": "https://example.com/video", "ext": "mp4"}
        def transfer(filename: str, stream_info: dict) -> tuple[bool, bool]:
            """将模拟 stream_info 写入 filename，返回传输成功标记。"""
            Path(filename).write_bytes(b"stream")
            return True, True
        def mux(executable: str, sources: list[Path], formats: list[dict], target: Path, stem: str) -> Path:
            """模拟 executable 将 sources/formats 封装至 target/stem，返回视频路径。"""
            result = target / f"{stem}.mp4"
            result.write_bytes(sources[0].read_bytes())
            return result
        downloader.dl.side_effect = transfer
        with patch.object(tool, "load_settings", return_value=self.settings), patch.object(tool, "resolve_ffmpeg", return_value="ffmpeg"), patch.object(tool, "authenticated_session", return_value=session), patch.object(tool, "ensure_login", return_value={"uname": "test"}), patch.object(tool, "make_downloader", return_value=downloader), patch.object(tool, "list_subtitles", return_value=[]), patch.object(tool, "print_video_info"), patch.object(tool, "mux_streams", side_effect=mux), patch.object(metadata, "probe_media", return_value={"errors": []}):
            for bvid in self.ids:
                target = tool.VideoTarget(f"https://www.bilibili.com/video/{bvid}?p=1", 123, 1, 1)
                remote = {**self.season, "bvid": bvid, "title": f"投稿{bvid}", "videos": 1, "owner": {"mid": 1, "name": "test"}}
                downloader.extract_info.return_value = {"id": f"{bvid}_p1", "title": f"投稿{bvid}", "requested_formats": [fmt]}
                with patch.object(tool, "resolve_targets", return_value=[target]), patch.object(tool, "api_json", return_value={"data": remote}):
                    self.assertEqual(tool.run(tool.parse_args(["--url", bvid])), 0)
        folder = self.root / "测试合集_合集123"
        self.assertEqual(len(list(folder.glob("*.mp4"))), 2)
        self.assertEqual(len(list(folder.glob("*_video_info.json"))), 2)

    def test_conflict_preserves_original_files(self) -> None:
        """验证合集存在同名文件时拒绝覆盖并保留原投稿和记录；无返回值。"""
        record = self.old_post(self.ids[0])
        folder = self.root / "测试合集_合集123"
        folder.mkdir()
        target = folder / f"投稿_{self.ids[0]}_p1.mp4"
        target.write_bytes(b"existing")
        with self.assertRaises(tool.ToolError):
            tool.prepare_collection(self.settings, self.season)
        self.assertEqual(target.read_bytes(), b"existing")
        self.assertTrue(record.path.exists())
        self.assertEqual(record.resolve_file(record.data["files"][0]).read_bytes(), b"video")

    def test_record_write_failure_rolls_back_moved_files(self) -> None:
        """验证合集记录写入失败时原投稿文件及原记录均恢复；无返回值。"""
        record = self.old_post(self.ids[0])
        folder = self.root / "测试合集_合集123"
        with patch.object(record, "save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            metadata.move_to_collection(record, folder)
        self.assertTrue(record.path.is_file())
        self.assertEqual(record.resolve_file(record.data["files"][0]).read_bytes(), b"video")
        self.assertFalse((folder / f"{self.ids[0]}_video_info.json").exists())

    def test_unrelated_post_and_invalid_collection(self) -> None:
        """验证非合集投稿维持原目录，错误的合集数据不迁移文件；无返回值。"""
        record = self.old_post(self.ids[0])
        self.assertIsNone(tool.prepare_collection(self.settings, {"bvid": self.ids[0]}))
        with self.assertRaises(tool.ToolError):
            tool.prepare_collection(self.settings, {"bvid": self.ids[0], "ugc_season": {"id": 123, "title": "测试"}})
        self.assertTrue(record.path.exists())
