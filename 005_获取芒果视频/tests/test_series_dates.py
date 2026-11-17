"""验证上映日期解析、批次缓存和已有芒果目录的安全同步。"""

from io import BytesIO
import json
from pathlib import Path
from unittest.mock import Mock, patch

from local.client import NativeClient
from local.errors import ToolError
from local.migration import plan_series_dates, synchronize_series
from local.series import date_prefix, read_release_date
from local.storage import atomic_json
from tests.test_workflow import BaseCase


class Response(BytesIO):
    """为只读页面测试提供带 URL 的内存响应。"""

    def __init__(self, body: bytes, url: str):
        """保存页面 body 和最终 url；无返回值。"""
        super().__init__(body)
        self.url = url

    def geturl(self) -> str:
        """返回本响应的最终 URL；无参数。"""
        return self.url


class SeriesDateTests(BaseCase):
    """使用当前配置与合成官方页面验证日期的边界和缓存。"""

    def page(self, body: str, final_url: str | None = None):
        """将 body 设置为合成页面，final_url 可模拟跳转；返回记录请求的传输替身。"""
        settings = self.config["profile"]["series_metadata"]
        expected = settings["page_url"].format(clip_id="1")
        transport = Mock(network=self.config["network"])
        transport.open.side_effect = lambda url: Response(body.encode("utf-8"), final_url or expected)
        return transport

    def test_official_album_date_is_unambiguous(self):
        """同一日期的完整时间和短日期都可读取，其他时间字段不会参与选取；无返回值。"""
        settings = self.config["profile"]["series_metadata"]
        field = settings["release_date_field"]
        transport = self.page(f'{field}:"2000-01-02 00:00:00.0",otherTime:"2001-02-03",{field}:"2000-01-02"')
        self.assertEqual(read_release_date(transport, settings, "1"), "2000-01-02")

    def test_conflicting_invalid_and_redirected_dates_fail(self):
        """日期冲突、非法日期或跳到不同剧集时停止，不能推测日期；无返回值。"""
        settings = self.config["profile"]["series_metadata"]
        field = settings["release_date_field"]
        for body in (f'{field}:"2000-01-02",{field}:"2001-01-02"', f'{field}:"2000-02-30"', "missing"):
            with self.subTest(body=body), self.assertRaises(ToolError):
                read_release_date(self.page(body), settings, "1")
        with self.assertRaises(ToolError):
            read_release_date(self.page(f'{field}:"2000-01-02"', settings["page_url"].format(clip_id="2")), settings, "1")

    def test_one_album_date_is_fixed_within_client(self):
        """同剧多集仅请求一次专辑日期；无参数/返回值。"""
        client = NativeClient(self.config, self.cancel)
        with patch("local.series.read_release_date", side_effect=["2000-01-02", "2001-02-03"]) as fetch:
            self.assertEqual(client.series_release_date("1"), "2000-01-02")
            self.assertEqual(client.series_release_date("1"), "2000-01-02")
            self.assertEqual(client.series_release_date("2"), "2001-02-03")
            self.assertEqual(fetch.call_count, 2)


class SeriesMigrationTests(BaseCase):
    """验证同目录改名、索引同步和失败回滚。"""

    def setUp(self):
        """创建两个合成剧集与独立索引；使用实际命名配置，无返回值。"""
        super().setUp()
        self.config["output_path"].mkdir()
        self.config["work_dir"].mkdir()
        self.info = {"1": ("合成甲", "2000-01-02"), "2": ("合成乙", "2001-02-03")}
        self.client = Mock()
        self.client.catalog_page.side_effect = lambda clip, page: {"clipInfo": {"clipId": clip, "clipName": self.info[clip][0]}}
        self.client.series_release_date.side_effect = lambda clip: self.info[clip][1]
        self.original = []
        for clip, (title, _) in self.info.items():
            name = self.config["naming"]["series_template"].format(title=title, clip_id=clip)
            directory = self.config["output_path"] / name
            directory.mkdir()
            (directory / "001_原片名.mp4").write_bytes(clip.encode())
            self.original.append(directory)
        self.index_path = self.config["output_path"] / self.config["logging"]["index_file"]
        self.index = {"schema": 1, "videos": {"1/10": [{"path": self.original[0].name + "/001_原片名.mp4", "sha256": "x"}]}}
        atomic_json(self.index_path, self.index, self.config["logging"])

    def test_rename_updates_only_folder_and_index(self):
        """目录原位改名、视频文件名不变且索引相对路径同步；无返回值。"""
        other = self.config["output_path"] / "其他平台"
        other.mkdir()
        plan = plan_series_dates(self.config, self.client)
        report = synchronize_series(self.config, plan)
        self.assertEqual(len(report["moved"]), 2)
        self.assertEqual(report["updated_records"], 1)
        for item in plan:
            destination = self.config["output_path"] / item["target"]
            self.assertFalse((self.config["output_path"] / item["name"]).exists())
            self.assertEqual((destination / "001_原片名.mp4").read_bytes(), item["clip_id"].encode())
        self.assertTrue(other.is_dir())
        index = json.loads(self.index_path.read_text(encoding="utf-8"))
        self.assertEqual(index["videos"]["1/10"][0]["path"], date_prefix(self.config["naming"], self.info["1"][1]) + self.index["videos"]["1/10"][0]["path"])
        self.assertEqual(json.loads(Path(report["journal"]).read_text(encoding="utf-8"))["status"], "completed")
        self.assertFalse(synchronize_series(self.config, plan_series_dates(self.config, self.client))["moved"])

    def test_collision_does_not_move_any_directory(self):
        """目标路径被其他文件占用时不覆盖或部分改名；无返回值。"""
        plan = plan_series_dates(self.config, self.client)
        (self.config["output_path"] / plan[0]["target"]).write_bytes(b"existing")
        with self.assertRaises(ToolError):
            synchronize_series(self.config, plan)
        self.assertTrue(all(path.is_dir() for path in self.original))
        self.assertEqual(json.loads(self.index_path.read_text(encoding="utf-8")), self.index)

    def test_partial_move_failure_rolls_back_prior_directories(self):
        """第二个目录改名失败时撤回第一个，索引保持原值；无返回值。"""
        plan = plan_series_dates(self.config, self.client)
        rename = Path.rename

        def failure(path, target):
            """对第二个源 path 模拟占用，其他目标 target 正常原位改名；返回目标路径。"""
            if path.name == plan[1]["name"]:
                raise PermissionError("synthetic")
            return rename(path, target)

        with patch.object(Path, "rename", failure), self.assertRaises(PermissionError):
            synchronize_series(self.config, plan)
        self.assertTrue(all(path.is_dir() for path in self.original))
        self.assertEqual(json.loads(self.index_path.read_text(encoding="utf-8")), self.index)
