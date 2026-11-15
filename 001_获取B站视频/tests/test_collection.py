"""离线验证 B 站合集增量归档、旧投稿迁移及记录隔离。"""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bilibili_downloader import api, application, auth, errors, library, media, metadata, models, quality, workflow
from bilibili_downloader import downloader as download_service
from bilibili_downloader import subtitles as subtitle_service
from bilibili_downloader import config as configuration
import download_bilibili as cli_entry
from tests import PROJECT_DIR, fake_auth_snapshot


class CollectionTests(unittest.TestCase):
    """用临时目录和模拟原站合集验证多投稿共享目录。"""

    def setUp(self) -> None:
        """创建临时下载目录、两个 BV 号及模拟合集信息；无返回值。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.settings = replace(configuration.load_settings(PROJECT_DIR / "config.yaml"), output_dir=self.root, threads=1,
                                mix_quality=False)
        self.enterContext(patch.object(auth, 'snapshot_session', return_value=fake_auth_snapshot()))
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
        record.data['schema_version'] = 4
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
        folder = library.prepare_collection(self.settings, self.season)
        self.assertEqual(folder, self.root / "测试合集_合集123")
        self.assertFalse(first.path.parent.exists())
        self.assertFalse(second.path.parent.exists())
        for bvid in self.ids:
            record = library.get_video_info(self.settings, bvid)
            quality_folder = folder.with_name(folder.name + '_' + models.LEGACY_QUALITY.directory + '_分P')
            self.assertEqual(record.path, quality_folder / f"{bvid}_video_info.json")
            self.assertEqual(record.resolve_file(record.data["files"][0]), quality_folder / f"投稿_{bvid}_p1.mp4")
            self.assertEqual((quality_folder / f"投稿_{bvid}_p1.zh.srt").read_bytes(), b"subtitle")
            self.assertEqual((quality_folder / ".streams" / f"{bvid}_p1/0.raw.part").read_bytes(), b"partial")
            with patch.object(metadata, "probe_media", return_value={"video_codec": "h264", "errors": []}):
                record.refresh_local("ffmpeg")
            self.assertEqual(len(record.data["files"]), 1)
        self.assertEqual(metadata.discover_videos(self.root)[0], sorted(self.ids))
        self.assertEqual(library.prepare_collection(self.settings, self.season), folder)

    def test_new_member_uses_existing_collection_and_skips_repeat(self) -> None:
        """验证合集目录中新成员直接保存，重复下载根据独立记录跳过；无返回值。"""
        folder = self.root / "测试合集_合集123"
        folder.mkdir()
        self.assertEqual(library.prepare_collection(self.settings, self.season), folder)
        record = library.get_video_info(self.settings, self.ids[0], "投稿", 1, shared_folder=folder)
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
        with patch.object(media, "mux_streams", side_effect=mux), patch.object(metadata, "probe_media", return_value={"errors": []}):
            self.assertEqual(download_service.save_video(downloader, info, self.settings, "ffmpeg", "投稿", 1, source_cid=1)[0], folder.with_name(folder.name + '_' + models.LEGACY_QUALITY.directory + '_分P'))
            downloader.dl.reset_mock()
            download_service.save_video(downloader, info, self.settings, "ffmpeg", "投稿", 1, source_cid=1)
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
        selected_quality = models.Quality(80, '1080P')
        downloader.process_ie_result.side_effect = lambda info, download: {**info, 'requested_formats': [fmt]}
        with patch.object(configuration, "load_settings", return_value=self.settings), patch.object(media, "resolve_ffmpeg", return_value="ffmpeg"), patch.object(auth, "authenticated_session", return_value=session), patch.object(auth, "ensure_login", return_value={"uname": "test"}), patch.object(download_service, "make_downloader", return_value=downloader), patch.object(subtitle_service, "list_subtitles", return_value=[]), patch.object(workflow, "print_video_info"), patch.object(media, "mux_streams", side_effect=mux), patch.object(metadata, "probe_media", return_value={"errors": []}):
            for bvid in self.ids:
                target = models.VideoTarget(f"https://www.bilibili.com/video/{bvid}?p=1", 123, 1, 1)
                remote = {**self.season, "bvid": bvid, "title": f"投稿{bvid}", "videos": 1, "owner": {"mid": 1, "name": "test"}}
                downloader.extract_info.return_value = {"id": f"{bvid}_p1", "title": f"投稿{bvid}", "requested_formats": [fmt]}
                with patch.object(api, "resolve_targets", return_value=[target]), patch.object(api, "api_json", return_value={"data": remote}), patch.object(quality, 'available_qualities', return_value={80: selected_quality}), patch.object(quality, 'confirm_ceiling', return_value=selected_quality), patch.object(quality, 'select_quality', side_effect=lambda _downloader, info, _quality: {**info, 'selected_quality': {'code': 80, 'name': '1080P'}}):
                    self.assertEqual(application.run(cli_entry.parse_args(["--url", bvid])), 0)
        folder = self.root / "测试合集_合集123"
        quality_folder = folder.with_name(folder.name + '_' + selected_quality.directory + '_分P')
        self.assertEqual(len(list(quality_folder.glob("*.mp4"))), 2)
        self.assertEqual(len(list(quality_folder.glob("*_video_info.json"))), 2)

    def test_conflict_preserves_original_files(self) -> None:
        """验证合集存在同名文件时拒绝覆盖并保留原投稿和记录；无返回值。"""
        record = self.old_post(self.ids[0])
        record = library.get_video_info(self.settings, self.ids[0])
        folder = self.root / "测试合集_合集123"
        folder.mkdir()
        quality_folder = folder.with_name(folder.name + '_' + models.LEGACY_QUALITY.directory + '_分P')
        quality_folder.mkdir()
        target = quality_folder / f"投稿_{self.ids[0]}_p1.mp4"
        target.write_bytes(b"existing")
        with self.assertRaises(errors.ToolError):
            library.prepare_collection(self.settings, self.season)
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
        self.assertIsNone(library.prepare_collection(self.settings, {"bvid": self.ids[0]}))
        with self.assertRaises(errors.ToolError):
            library.prepare_collection(self.settings, {"bvid": self.ids[0], "ugc_season": {"id": 123, "title": "测试"}})
        self.assertTrue(record.path.exists())
