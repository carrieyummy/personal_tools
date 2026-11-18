"""验证配置相对路径、敏感字段隔离和 UPS 流结构；测试值从当前研究配置构造。"""

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from lib.probe import analyze_ups, load_config, resolve_path, scan_cache, stream_summary

CONFIG = Path(__file__).resolve().parents[1] / "configs/research.yaml"


class ProbeTests(unittest.TestCase):
    """验证只读研究的主要边界；无构造参数，结果通过 unittest 断言报告。"""

    def setUp(self) -> None:
        """读取当前用户配置；无参数；将配置保存到实例，不验证用户可修改的具体取值。"""
        self.config = load_config(CONFIG)

    def test_sample_comes_from_download_list(self) -> None:
        """确认样本来自清单；无参数和返回值。"""
        tasks = yaml.safe_load(self.config["download_list"].read_text(encoding="utf-8"))
        task = tasks["download_list"][self.config["sample_index"]]
        self.assertEqual(self.config["sample"]["url"], task)

    def test_relative_path_uses_config_base(self) -> None:
        """确认相对路径基准；无参数和返回值。"""
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(self.config["download_list"], resolve_path(raw["download_list"], CONFIG.parent))

    def test_nested_streams_never_export_secrets(self) -> None:
        """确认递归响应只保留元数据；无参数和返回值。"""
        fields = self.config["cache"]["stream_fields"]
        stream = {k: k for k in fields}
        stream.update({"m3u8_url": "private-url", "key": "private-key",
                       "segs": [{"cdn_url": "private-segment"}]})
        result = stream_summary({"data": {"data": {"stream": [stream]}}}, fields + ["key", "m3u8_url"])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["has_m3u8_url"])
        self.assertEqual(result[0]["segment_count"], len(stream["segs"]))
        self.assertNotIn("private-", json.dumps(result))

    def test_missing_streams_do_not_claim_success(self) -> None:
        """确认错误响应没有流时返回空结果；无参数和返回值。"""
        self.assertEqual(stream_summary({"error": {"code": "denied"}}, self.config["cache"]["stream_fields"]), [])

    def test_cache_handles_binary_prefix_and_budget(self) -> None:
        """确认带缓存头的 JSON 可检出且预算生效；无参数和返回值。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            field = self.config["cache"]["stream_fields"][0]
            marker = self.config["cache"]["json_markers"][0]
            outer_key = marker.split('"')[1]
            payload = json.dumps({outer_key: {"stream": [{field: field}]}}).encode()
            raw = b"\x00HTTP/1.1 200 OK\x00" + payload
            (root / "cache_entry").write_bytes(raw)
            config = dict(self.config)
            config["cache"] = dict(self.config["cache"], roots=[root], max_file_bytes=len(raw), max_total_bytes=len(raw))
            self.assertEqual(len(scan_cache(config)["stream_documents"]), 1)
            config["cache"]["max_total_bytes"] = len(raw) - 1
            self.assertTrue(scan_cache(config)["budget_exhausted"])

    def test_ups_does_not_claim_independent_file(self) -> None:
        """确认接口响应不能被误报为独立成品；无参数和返回值。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ups.json"
            path.write_text(json.dumps({"data": {"stream": []}}), encoding="utf-8")
            self.assertFalse(analyze_ups(self.config, path)["independent_file_verified"])


if __name__ == "__main__":
    unittest.main()
