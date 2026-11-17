"""隔离检查播放缓存覆盖及模块核验，不运行客户端下载、播放控制或公网请求。"""

from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import yaml

from qsv_local.playback.cache import cache_snapshot, coverage
from qsv_local.playback.configuration import read_profile
from qsv_local.playback.metadata import public_duration
from qsv_local.playback.download import segment_ranges
from qsv_local.playback.media import parse_manifest, signed_segment
from qsv_local.playback.session import is_target_dash, replay, response_summary


class PlaybackCacheResearchTests(unittest.TestCase):
    """使用合成缓存索引和当前配置核对研究边界。"""

    def setUp(self):
        """读取当前研究协议配置，并建立不依赖实际影片规格的两段合成索引；无返回值。"""
        path = Path(__file__).resolve().parents[1] / "configs/playback.yaml"
        self.profile = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.rows = [{"TVID": "123", "VID": "synthetic_video", "FILE_TYPE": "0", "SEG_INDEX": str(index),
                      "SEG_TOTAL_COUNT": "2", "FILE_SIZE": "10", "FILE_BLOCK_SIZE": "10", "CACHED_BLOCKS": "1",
                      "CACHED_BYTES": "10", "FIRST_BLOCK": "0", "LAST_BLOCK": "0"} for index in range(2)]

    def test_contiguous_full_segments_have_index_coverage(self):
        """所有分段均有完整连续块时，仅标记索引覆盖成立；无返回值。"""
        value = coverage(self.rows)[0]
        self.assertTrue(value["index_coverage_complete"])
        self.assertEqual(value["complete_segments"], list(range(len(self.rows))))

    def test_missing_segment_is_not_complete(self):
        """索引缺少一个分段时不误报整片完整；无返回值。"""
        self.assertFalse(coverage(self.rows[:1])[0]["index_coverage_complete"])

    def test_partial_block_is_not_complete(self):
        """有分段记录但实际缓存字节不足时不误报完整；无返回值。"""
        self.rows[-1]["CACHED_BYTES"] = str(int(self.rows[-1]["FILE_SIZE"]) - 1)
        self.assertFalse(coverage(self.rows)[0]["index_coverage_complete"])

    def test_duplicate_segment_is_rejected(self):
        """同一来源的分段重复时拒绝判断；无返回值。"""
        with self.assertRaisesRegex(ValueError, "不唯一"):
            coverage(self.rows + [deepcopy(self.rows[0])])

    def test_changing_segment_total_is_rejected(self):
        """同一来源的分段总数不一致时拒绝判断；无返回值。"""
        self.rows[-1]["SEG_TOTAL_COUNT"] = str(len(self.rows) + 1)
        with self.assertRaisesRegex(ValueError, "总数不一致"):
            coverage(self.rows)

    def test_unverified_module_is_rejected_before_attach(self):
        """不匹配的原生模块摘要在挂接前被拒绝；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = self.profile["native"]
            library = root / settings["module"]
            library.write_bytes(b"unverified_synthetic_module")
            process = MagicMock()
            process.pid = 123
            process.info = {"pid": 123, "name": settings["process_name"], "exe": str(root / settings["process_name"])}
            process.memory_maps.return_value = [SimpleNamespace(path=str(library))]
            processes, native = MagicMock(), MagicMock()
            processes.process_iter.return_value = [process]
            processes.NoSuchProcess, processes.AccessDenied = ProcessLookupError, PermissionError
            profile = {"native": settings, "client_root": root}
            with patch.dict("sys.modules", {"frida": native, "psutil": processes}):
                with self.assertRaisesRegex(RuntimeError, "未经"):
                    cache_snapshot(profile, [self.rows[0]["TVID"]])
            native.attach.assert_not_called()

    def test_profile_does_not_require_offline_downloads(self):
        """播放缓存研究只从主配置读取客户端适配引用，无离线下载库或下载目录时仍可解析；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            raw = deepcopy(self.profile)
            raw.update(main_config="main.yaml", download_list="links.yaml", cache_root="cache", report_dir="reports")
            (root / raw["cache_root"]).mkdir()
            (root / "client").mkdir()
            (root / raw["main_config"]).write_text(yaml.safe_dump({"native_profile": "native.yaml"}), encoding="utf-8")
            (root / "native.yaml").write_text(yaml.safe_dump({"dll_root": "client"}), encoding="utf-8")
            path = root / "research.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            value = read_profile(path)
            self.assertEqual(value["client_root"], root / "client")
            self.assertNotIn("download_guard", value)
            (root / raw["cache_root"]).rmdir()
            self.assertEqual(read_profile(path)["client_root"], root / "client")


class PlaybackSessionResearchTests(unittest.TestCase):
    """用合成会话验证目标集、调度签名对应和报告中的敏感信息边界。"""

    def setUp(self):
        """读取用户当前端点配置，构造不对应真实视频的内存请求；无返回值。"""
        path = Path(__file__).resolve().parents[1] / "configs/playback.yaml"
        self.profile = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.settings = self.profile["playback"]
        self.url = "https://" + self.settings["dash_host"] + self.settings["dash_path"] + "?tvid=123"
        self.host = "synthetic" + self.settings["resource_host_suffix"]

    def test_wrong_episode_and_duplicate_ids_are_rejected(self):
        """历史续播到其他集或出现多个目标编号时不接受响应；无返回值。"""
        self.assertTrue(is_target_dash(self.url, self.settings, "123"))
        self.assertFalse(is_target_dash(self.url, self.settings, "456"))
        self.assertFalse(is_target_dash(self.url + "&tvid=123", self.settings, "123"))
        self.assertFalse(is_target_dash(self.url.replace(self.settings["dash_host"], "unrelated.invalid"), self.settings, "123"))

    def test_original_auth_is_only_sent_to_dash_host(self):
        """原生请求头不能用于另一域名，且在任何网络调用前失败；无返回值。"""
        record = {"url": self.url.replace(self.settings["dash_host"], "unrelated.invalid"), "headers": ["pck: secret"]}
        with patch("qsv_local.playback.session.open_network") as network:
            with self.assertRaisesRegex(ValueError, "只允许"):
                replay(record, self.profile)
        network.assert_not_called()

    def test_report_excludes_tokens_addresses_and_response_values(self):
        """报告只保留白名单，不把嵌套授权或响应扩展字段写入文件；无返回值。"""
        payload = {"code": "A00000", "data": {"p": {"private": "sensitive_p"}, "program": {"video": [
            {"vid": "synthetic_video", "_selected": True, "ml": "https://private.invalid/sensitive_url",
             "drm": {"ticket": "sensitive_ticket"}, "unknown": "sensitive_extension"}]}}}
        text = str(response_summary(payload))
        for value in ("sensitive_p", "sensitive_url", "sensitive_ticket", "sensitive_extension"):
            self.assertNotIn(value, text)

    def test_incomplete_playlist_cannot_be_treated_as_full_episode(self):
        """未结束的清单不能成为完整点播来源；无返回值。"""
        with self.assertRaisesRegex(ValueError, "已结束"):
            parse_manifest(b"#EXTM3U\n#EXTINF:2,\npart.ts\n", "https://" + self.host + "/list.m3u8", self.settings["resource_host_suffix"])

    def test_playlist_cannot_escape_verified_media_hosts(self):
        """清单中的外域片段被拒绝，即使基础地址合法；无返回值。"""
        body = b"#EXTM3U\n#EXTINF:2,\nhttps://unrelated.invalid/part.ts\n#EXT-X-ENDLIST"
        with self.assertRaisesRegex(ValueError, "媒体地址"):
            parse_manifest(body, "https://" + self.host + "/list.m3u8", self.settings["resource_host_suffix"])

    def test_dispatch_signature_requires_exact_filename(self):
        """不同物理文件不能复用签名；无返回值。"""
        segment = {"url": "https://" + self.host + "/part.ts?start=0&end=9"}
        with self.assertRaisesRegex(ValueError, "缺少"):
            signed_segment(segment, {"mk": {"other.ts": "qd_sc=synthetic"}})
        self.assertTrue(signed_segment(segment, {"mk": {"part.ts": "qd_sc=synthetic"}}).endswith("&qd_sc=synthetic"))

    def test_dispatch_does_not_guess_duplicate_parameter_precedence(self):
        """地址与调度签名有重复参数时停止，不覆盖可能影响签名的字段；无返回值。"""
        with self.assertRaisesRegex(ValueError, "重复参数"):
            signed_segment({"url": "https://" + self.host + "/part.ts?start=0"}, {"mk": {"part.ts": "start=1"}})

    def test_embedded_dispatch_requires_complete_authorization_fields(self):
        """新格式同时要求清单内 qd_tc、uea 标记和 ms 会话签名；缺少任一部分均拒绝；无返回值。"""
        url = "https://" + self.host + "/part.ts?qd_tc=synthetic&qd_rt=1&qd_etd=2&qd_tvid=123"
        source = {"uea": 1, "ms": "qd_sc=synthetic"}
        self.assertEqual(signed_segment({"url": url}, source), url + "&" + source["ms"])
        with self.assertRaisesRegex(ValueError, "缺少"):
            signed_segment({"url": url.rsplit("&", 1)[0]}, source)
        with self.assertRaisesRegex(ValueError, "缺少"):
            signed_segment({"url": url}, {"uea": 1})
        with self.assertRaisesRegex(ValueError, "缺少"):
            signed_segment({"url": url}, {"ms": source["ms"]})

    def test_range_coverage_and_declared_size_are_both_required(self):
        """按调度协议的半开区间核对连续范围及总字节数，拒绝缺口和重叠；无返回值。"""
        base = "https://" + self.host + "/part.ts"
        values = [{"url": base + "?start=0&end=10"}, {"url": base + "?start=10&end=20"}]
        self.assertEqual(segment_ranges(values, 20), [10, 10])
        with self.assertRaisesRegex(ValueError, "字节数"):
            segment_ranges(values, 21)
        for start in (9, 11):
            with self.assertRaisesRegex(ValueError, "不连续"):
                segment_ranges([values[0], {"url": base + f"?start={start}&end=20"}], 20)

    def test_public_clock_duration_is_validated(self):
        """公开资料时钟格式与秒数均可识别，非法分钟/秒值不进入全片时长验证；无返回值。"""
        self.assertEqual(public_duration("02:03"), 123)
        self.assertEqual(public_duration("01:02:03"), 3723)
        with self.assertRaisesRegex(ValueError, "时长格式"):
            public_duration("02:60")


if __name__ == "__main__":
    unittest.main()
