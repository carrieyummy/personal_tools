"""验证客户端上下文对照与播放日志脱敏；使用合成数据，不读取真实会话或发网络请求。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, urlencode

from lib.client_context import load_client_context
from lib.native import sample_vid
from lib.online import online_probe
from lib.playback_log import media_request_fields, scan_playback_log
from lib.probe import load_config

CONFIG = Path(__file__).resolve().parents[1] / "configs/research.yaml"


class ContextTests(unittest.TestCase):
    """覆盖日志来源、对照请求和报告边界；无构造参数，结果由 unittest 报告。"""

    def setUp(self) -> None:
        """加载当前研究配置；无参数或返回值，样本和对照组均由用户配置决定。"""
        self.config = load_config(CONFIG)

    def test_context_uses_latest_log_and_rejects_version_mismatch(self) -> None:
        """校验最新完整日志及版本绑定，并检查摘要不带设备值；无参数或返回值。"""
        options = self.config["client_context"]
        version = "1.2.3.4"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            log = root / options["log_path"]
            log.parent.mkdir(parents=True)
            lines = []
            for device in ("fake-old-device", "fake-current-device"):
                lines.append(f"[2026-01-01 00:00:00.000] userAgent=Mozilla/5.0 UTDID {device};"
                             f"ZREAL 0;BTYPE fake-product;BRAND fake-brand;BUILDVER {version});"
                             "LOCALV2 ON, process.argv=[]")
            log.write_text("\n".join(lines), encoding="utf-8")
            bits = sum(1 << value for value in options["capability_bits"].values())
            state = str(bits)
            for key in reversed(options["capability_path"]):
                state = {key: state}
            (root / options["state_path"]).write_text(json.dumps(state), encoding="utf-8")
            config = dict(self.config, user_data=root)
            result = load_client_context(config, version)
            self.assertEqual(result["device"], "fake-current-device")
            self.assertTrue(all(result["summary"]["cached_capabilities"].values()))
            self.assertNotIn("fake-", json.dumps(result["summary"]))
            with self.assertRaises(ValueError):
                load_client_context(config, version + ".0")

    def test_missing_user_agent_does_not_fall_back(self) -> None:
        """缺失完整日志时明确失败；无参数或返回值。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            log = root / self.config["client_context"]["log_path"]
            log.parent.mkdir(parents=True)
            log.write_text("incomplete log", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_client_context(dict(self.config, user_data=root), "test-version")

    def test_online_cases_use_configured_context_without_exporting_values(self) -> None:
        """验证每组实际发出的 UA、设备和硬件字段，并检查报告脱敏；无参数或返回值。"""
        context = {"user_agent": "fake-private-user-agent", "device": "fake-private-device",
                   "biz_params": {key: "fake-private-hardware" for key in
                                  self.config["client_context"]["biz_field_mapping"]}, "summary": {}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package = root / "resources/app/package.json"
            package.parent.mkdir(parents=True)
            package.write_text(json.dumps({"version": "1.2.3", "buildnumber": "4"}), encoding="utf-8")
            with patch("lib.online.load_session"), patch("lib.online.cookie_value", return_value="fake-cookie"), \
                    patch("lib.online.load_client_context", return_value=context), \
                    patch("lib.online.native_signature", return_value={"ckey": "fake-key", "client_ts": "1"}), \
                    patch("lib.online.request_mtop", return_value={"ret": ["SUCCESS"], "data": {}}) as request:
                result = online_probe(dict(self.config, client_dir=root))
            cases = self.config["network"]["ups"]["cases"]
            self.assertEqual(len(request.call_args_list), len(cases) + 2)
            for case, call in zip(cases, request.call_args_list[2:]):
                payload, ua = call.args[3:5]
                steal = json.loads(payload["steal_params"])
                biz = json.loads(payload["biz_params"])
                expected_device = context["device"] if case["device_source"] == "client_log" else "fake-cookie"
                self.assertEqual(steal["utid"], expected_device)
                self.assertEqual(ua == context["user_agent"], case["user_agent_source"] == "client_log")
                for key, value in context["biz_params"].items():
                    self.assertEqual(biz.get(key), value if case["client_hardware_fields"] else case["biz_params"].get(key))
            self.assertNotIn("fake-", json.dumps(result))

    def test_media_url_matches_exact_sample_and_never_exports_secrets(self) -> None:
        """校验外层编码 URL、精确样本匹配和公开字段限制；无参数或返回值。"""
        vid = sample_vid(self.config)
        url = "https://media.invalid/play?" + urlencode({"vid": vid, "type": "fixture_hbr",
                                                        "vkey": "fake-private-key", "utid": "fake-private-device"})
        rounds = self.config["playback_log"]["decode_rounds"]
        result = media_request_fields(quote(url, safe=":/"), vid, rounds)
        self.assertEqual(result, {"type": "fixture_hbr"})
        self.assertIsNone(media_request_fields(url, vid + "different", rounds))
        self.assertNotIn("fake-", json.dumps(result))

    def test_playback_budget_and_sample_attribution(self) -> None:
        """确认扫描预算、不同剧集隔离和报告不含原始日志；无参数或返回值。"""
        vid = sample_vid(self.config)
        url = "https://media.invalid/play?" + urlencode({"vid": vid, "type": "fixture_hbr", "vkey": "fake-secret"})
        other_url = "https://media.invalid/play?" + urlencode({"vid": vid + "different", "type": "fixture_hbr"})
        raw = ("[info 2026-01-01 00:00:00 ] " + url + "\n" + other_url + "\n").encode()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "fixture.log").write_bytes(raw)
            options = dict(self.config["playback_log"], glob="*.log", max_file_bytes=len(raw), max_total_bytes=len(raw))
            config = dict(self.config, native_data=root, playback_log=options)
            result = scan_playback_log(config)
            self.assertEqual(result["files"][0]["requests"][0]["type"], "fixture_hbr")
            self.assertEqual(result["files"][0]["requests"][0]["occurrences"], 1)
            self.assertNotIn("fake-secret", json.dumps(result))
            self.assertFalse(result["independent_file_verified"])
            options["max_total_bytes"] -= 1
            self.assertTrue(scan_playback_log(config)["budget_exhausted"])
