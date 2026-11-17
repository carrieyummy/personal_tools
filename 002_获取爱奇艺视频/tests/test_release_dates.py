"""隔离验证专辑上映日期、目录前缀及历史成品复用，不接触当前下载目录或客户端。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from qsv_local.playback.albums import AlbumMetadataCache
from qsv_local.playback.configuration import read_profile
from qsv_local.playback.download import manifest_identity
from qsv_local.playback.naming import validate_date_prefix
from qsv_local.playback.outputs import reusable_output, target_path
from qsv_local.storage import atomic_json, fingerprint


class ReleaseDateTests(unittest.TestCase):
    """从当前配置构造合成资料，验证日期来源、批次缓存和命名范围。"""

    def setUp(self):
        """读取配置副本并准备跨 UTC 日期边界的合成专辑；无返回值。"""
        self.root = Path(__file__).resolve().parents[1]
        self.config = yaml.safe_load((self.root / "configs/config.yaml").read_text(encoding="utf-8"))
        self.profile = yaml.safe_load((self.root / "configs/playback.yaml").read_text(encoding="utf-8"))
        self.cache = AlbumMetadataCache()
        self.day = date(2000, 1, 2)
        zone = timezone(timedelta(minutes=self.profile["network"]["album_date_utc_offset_minutes"]))
        local = datetime.combine(self.day, datetime.min.time(), zone)
        self.stamp = int((local.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1000)
        self.payload = {"code": "A00000", "data": {"albumId": "123", "publishTime": self.stamp,
                                                    "period": "2002-03-04", "issueTime": self.stamp + 86400000}}
        self.video = {"album_id": "123", "title": "合成剧", "episode": 1, "episode_title": "合成剧第1集",
                      "tvid": "456", "vid": "synthetic", "duration_seconds": 20, "release_date": self.day.isoformat()}
        self.source = {"vid": "synthetic_source", "scrsz": "16x16", "vsize": 20}

    def response(self, *args, **kwargs):
        """为 args/kwargs 指定的合成请求返回独立字节流；只包含本测试的公开专辑资料。"""
        return BytesIO(json.dumps(self.payload).encode())

    def test_album_publish_time_uses_configured_timezone_and_is_cached(self):
        """只使用专辑 publishTime，同一专辑并发多次读取仍只请求一次，返回副本互不影响；无返回值。"""
        with patch("qsv_local.playback.albums.open_network", side_effect=self.response) as network:
            with ThreadPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(lambda _: self.cache.get(self.profile, "123"), range(4)))
        self.assertEqual(network.call_count, 1)
        self.assertEqual(values[0], {"release_date": self.day.isoformat(), "album_publish_time": self.stamp})
        values[0]["release_date"] = "changed"
        self.assertEqual(values[1]["release_date"], self.day.isoformat())

    def test_missing_publish_time_never_uses_period_or_issue_time(self):
        """缺少上映时间不能用更新日期、单集日期或当前时间代替；无返回值。"""
        for value in (None, 0, True, str(self.stamp), 1.5):
            with self.subTest(value=value):
                self.payload["data"]["publishTime"] = value
                with patch("qsv_local.playback.albums.open_network", side_effect=self.response):
                    with self.assertRaisesRegex(ValueError, "上映日期"):
                        AlbumMetadataCache().get(self.profile, "123")

    def test_album_identity_and_date_range_are_checked(self):
        """拒绝其他专辑或超出日期范围的时间，不向命名流程提供猜测结果；无返回值。"""
        self.payload["data"]["albumId"] = "999"
        with patch("qsv_local.playback.albums.open_network", side_effect=self.response):
            with self.assertRaisesRegex(ValueError, "专辑编号"):
                self.cache.get(self.profile, "123")
        self.payload["data"]["albumId"] = "123"
        self.payload["data"]["publishTime"] = 10**30
        with patch("qsv_local.playback.albums.open_network", side_effect=self.response):
            with self.assertRaisesRegex(ValueError, "范围"):
                self.cache.get(self.profile, "123")

    def test_pre_epoch_date_is_supported_without_windows_timestamp_calls(self):
        """早于 Unix 纪元的有效上映时间不依赖 Windows 的负数时间戳接口；无返回值。"""
        expected = date(1960, 1, 2)
        zone = timezone(timedelta(minutes=self.profile["network"]["album_date_utc_offset_minutes"]))
        instant = datetime.combine(expected, datetime.min.time(), zone).astimezone(timezone.utc)
        self.payload["data"]["publishTime"] = int((instant - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1000)
        with patch("qsv_local.playback.albums.open_network", side_effect=self.response):
            value = self.cache.get(self.profile, "123")
        self.assertEqual(value["release_date"], expected.isoformat())

    def test_prefix_changes_only_the_folder_and_invalid_dates_fail(self):
        """日期只作用于文件夹，原视频模板及文件名保持不变，无效日期明确报错；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            config = deepcopy(self.config)
            config["output_path"] = str(Path(directory) / "output")
            old = target_path(config, Path(directory) / "main.yaml", self.video, self.source, with_date=False)
            current = target_path(config, Path(directory) / "main.yaml", self.video, self.source)
            prefix = config["naming"]["playback_date_prefix"].format(release_date=self.day)
            self.assertEqual(current.parent.name, prefix + old.parent.name)
            self.assertEqual(current.name, old.name)
            with self.assertRaisesRegex(ValueError, "上映日期"):
                target_path(config, Path(directory) / "main.yaml", {**self.video, "release_date": "2000-02-30"}, self.source)

    def test_unsafe_or_wrong_prefix_placeholders_are_rejected(self):
        """前缀不允许路径分隔符、空白或其他命名字段；无返回值。"""
        validate_date_prefix(self.config["naming"])
        for value in ("{title}_", "{release_date:%Y/%m/%d}_", "{release_date} ", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_date_prefix({**self.config["naming"], "playback_date_prefix": value})

    def test_date_does_not_change_media_resume_identity(self):
        """目录日期变化不重置媒体范围及内容身份，已有分段仍可校验复用；无返回值。"""
        rows = [{"url": "https://synthetic.invalid/media.ts?start=0&end=20", "duration": 20}]
        old_video = {key: value for key, value in self.video.items() if key != "release_date"}
        self.assertEqual(manifest_identity(self.profile, old_video, self.source, rows), manifest_identity(self.profile, self.video, self.source, rows))

    def test_legacy_output_is_reused_in_place_without_creating_date_folder(self):
        """已登记旧成品仍需核对摘要且留在原位，日期前缀不导致复制、移动或重复下载；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = deepcopy(self.config)
            config.update(output_path=str(root / "output"), work_dir=str(root / "work"))
            profile = {**self.profile, "main_config": root / "main.yaml"}
            old = target_path(config, profile["main_config"], self.video, self.source, with_date=False)
            desired = target_path(config, profile["main_config"], self.video, self.source)
            old.parent.mkdir(parents=True)
            old.write_bytes(b"registered verified media")
            identity = {"sha256": "synthetic identity"}
            proof = {"record_type": "playback_download", "schema_version": 1, "status": "completed", "source_identity": identity,
                     "media_verified": True, "verification": {"decode_exit_code": 0, "frames": 1}, "output": str(old),
                     "output_fingerprint": fingerprint(old, config["copy_buffer_bytes"]), "video": {key: value for key, value in self.video.items() if key != "release_date"}}
            record = old.with_suffix(".playback.json")
            atomic_json(record, proof)
            before = (old.stat().st_mtime_ns, record.read_bytes())
            media = {"width": 16, "height": 16, "duration_seconds": 20, "errors": []}
            with patch("qsv_local.playback.outputs.probe_media", return_value=media), patch("qsv_local.playback.outputs.require_same_metadata"):
                result = reusable_output(profile, config, self.video, self.source, identity, {"rank": 1, "native_selection_verified": True})
            self.assertEqual(result["status"], "reused")
            self.assertEqual(Path(result["output"]), old)
            self.assertTrue(result["legacy_directory"])
            self.assertFalse(desired.parent.exists())
            self.assertEqual((old.stat().st_mtime_ns, record.read_bytes()), before)

    def test_profile_holds_a_copy_of_naming_settings_for_each_run(self):
        """启动后修改临时主配置不改变该批次的命名快照；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = deepcopy(self.config)
            main["native_profile"] = str(self.root / "configs/native_v5.yaml")
            main_path = root / "main.yaml"
            main_path.write_text(yaml.safe_dump(main, allow_unicode=True), encoding="utf-8")
            raw = deepcopy(self.profile)
            raw.update(main_config=str(main_path), download_list=str(self.root / "download_list/download_list.yaml"), cache_root=str(root / "cache"), report_dir=str(root / "reports"))
            path = root / "playback.yaml"
            path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            running = read_profile(path)
            main["naming"]["playback_date_prefix"] += "changed_"
            main_path.write_text(yaml.safe_dump(main, allow_unicode=True), encoding="utf-8")
            later = read_profile(path)
            self.assertEqual(running["_naming_snapshot"], self.config["naming"])
            self.assertNotEqual(running["_naming_snapshot"], later["_naming_snapshot"])


if __name__ == "__main__":
    unittest.main()
