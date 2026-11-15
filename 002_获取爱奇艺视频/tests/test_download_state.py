"""验证下载保护发生在所有视频内容读取之前，状态不明确时禁止转换。"""

from pathlib import Path
import sqlite3
from unittest.mock import patch

from qsv_local.discovery import discover
from qsv_local.download_state import DownloadStatus
from tests.support import ProjectCase


class DownloadGuardTests(ProjectCase):
    """使用真实临时 SQLite 元数据及受监控的文件打开操作验证保护行为。"""

    def assert_not_opened(self, blocked: Path):
        """返回拦截 blocked 内容访问的 patch 上下文；其他 Path.open 参数原样传递。"""
        original = Path.open

        def guarded(path, *args, **kwargs):
            """拒绝打开受保护 path；args/kwargs 为原文件接口参数，返回其他文件的流对象。"""
            if path.resolve() == blocked.resolve():
                raise AssertionError("未完成视频不允许打开")
            return original(path, *args, **kwargs)

        return patch.object(Path, "open", guarded)

    def test_partial_suffix_is_not_opened_or_converted(self):
        """下载临时文件只按名称排除，不读取内容或建立视频目录索引；无参数或返回值。"""
        original = self.source("incomplete/video.qsv")
        partial = original.with_name(original.name + self.config["download_guard"]["temporary_suffixes"][0])
        original.rename(partial)
        with self.assert_not_opened(partial):
            result, converter = self.batch()
        self.assertEqual((result["total"], result["pending_downloads"], result["failed"]), (0, 1, 0))
        converter.assert_not_called()
        self.assertFalse((self.output / "incomplete/video_info.json").exists())

    def test_active_metadata_overrides_completed_even_with_final_filename(self):
        """正式后缀仍被标记下载中时，不打开、不哈希、不转换；其他完成文件照常处理；无参数或返回值。"""
        active = self.source("incomplete/video.qsv")
        self.source("finished/video.qsv")
        self.downloading.add(active)
        self.write_downloads()
        with self.assert_not_opened(active):
            result, converter = self.batch()
        self.assertEqual((result["completed"], result["pending_downloads"]), (1, 1))
        converter.assert_called_once()
        self.assertFalse((self.output / "incomplete/video_info.json").exists())

    def test_completed_file_with_partial_companion_is_deferred(self):
        """同名下载临时文件存在时，已登记的正式文件也暂不读取；无参数或返回值。"""
        source = self.source()
        partial = source.with_name(source.name + self.config["download_guard"]["temporary_suffixes"][0])
        partial.write_bytes(b"downloading")
        with self.assert_not_opened(source), self.assert_not_opened(partial):
            result, converter = self.batch()
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["pending_downloads"], 2)
        converter.assert_not_called()

    def test_unknown_final_file_is_deferred_without_opening(self):
        """没有客户端下载完成记录的正式 QSV 状态不明，禁止打开；无参数或返回值。"""
        unknown = self.input / "unknown.qsv"
        unknown.write_bytes(b"possibly-downloading")
        with self.assert_not_opened(unknown):
            result, converter = self.batch()
        self.assertEqual((result["total"], result["pending_downloads"]), (0, 1))
        converter.assert_not_called()

    def test_download_state_is_rechecked_before_processing(self):
        """扫描后若状态转为下载中，处理前再次检查并跳过，不读取内容；无参数或返回值。"""
        source = self.source()
        with (patch("qsv_local.batch.DownloadStatus") as status,
              self.assert_not_opened(source)):
            status.return_value.reason.return_value = "客户端记录仍在下载"
            result, converter = self.batch()
        self.assertEqual((result["total"], result["pending_downloads"], result["failed"]), (1, 1, 0))
        converter.assert_not_called()

    def test_missing_or_corrupt_metadata_prevents_video_access(self):
        """完成记录缺失或损坏时关闭处理入口，不猜测状态，不打开视频；无参数或返回值。"""
        source = self.source()
        settings = self.config["download_guard"]
        connection = sqlite3.connect(settings["database_path"])
        try:
            connection.execute("DELETE FROM table_file WHERE Name=?", (settings["completed_record"],))
            connection.commit()
        finally:
            connection.close()
        with self.assert_not_opened(source), self.assertRaisesRegex(ValueError, "缺少"):
            discover(self.config)

    def test_metadata_is_read_only(self):
        """只读状态查询不改变数据库内容或视频内容；无参数或返回值。"""
        source = self.source()
        path = self.config["download_guard"]["database_path"]
        before = path.read_bytes()
        self.assertIsNone(DownloadStatus(self.config).reason(source))
        self.assertEqual(path.read_bytes(), before)
