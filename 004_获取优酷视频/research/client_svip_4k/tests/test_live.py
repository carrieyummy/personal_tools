"""实时采样的请求来源、字段对照、脱敏和调试连接生命周期测试；不连接真实客户端或网络。"""

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from lib.live import LiveInspector, extract_live_payload, live_probe, request_summary
from lib.native import sample_vid
from lib.probe import load_config


class LiveTests(unittest.TestCase):
    """验证实时研究边界；无构造参数，断言结果由 unittest 报告。"""

    def setUp(self) -> None:
        """读取当前配置并构造虚拟请求；无参数或返回值，不使用真实会话。"""
        self.config = load_config(Path(__file__).resolve().parents[1] / "configs/research.yaml")
        self.groups = {
            "steal_params": {"ckey": "private-old-key", "utid": "private-device"},
            "biz_params": {"vid": sample_vid(self.config), "chipset": "fixture GPU",
                           "encryptR_client": "private-old-r1", "local_vid": "private-history"},
            "ad_params": {"os": "fixture OS", "osv": "fixture version", "needad": 1,
                          "adExtInfo": "private-ad-info"},
        }

    def make_url(self, groups: dict) -> str:
        """编码虚拟 UPS 地址；groups 为三组请求对象；返回仅供测试使用的地址。"""
        api = self.config["network"]["ups"]
        query = {"api": api["api"], "v": api["version"],
                 "data": json.dumps({key: json.dumps(value) for key, value in groups.items()})}
        return f"https://un-acs.youku.com/h5/{api['api']}/{api['version']}/?" + urlencode(query)

    def test_exact_sample_and_official_api_required(self) -> None:
        """接受真实 SDK 可省略的 steal vid，拒绝跨样本、跨站和不同 API；无参数或返回值。"""
        url = self.make_url(self.groups)
        self.assertEqual(extract_live_payload(self.config, url), self.groups)
        for section in ("biz_params", "steal_params"):
            groups = copy.deepcopy(self.groups)
            groups[section]["vid"] = sample_vid(self.config) + "-different"
            with self.assertRaises(ValueError):
                extract_live_payload(self.config, self.make_url(groups))
        for bad_url in (url.replace("un-acs.youku.com", "youku.com.invalid"),
                        url.replace("/h5/", "/different/"), url + "&data={}"):
            with self.assertRaises(ValueError):
                extract_live_payload(self.config, bad_url)

    def test_public_request_fields_exclude_secrets(self) -> None:
        """公开参数不包含签名、设备和观看记录；无参数或返回值。"""
        result = request_summary(self.groups)
        self.assertEqual(result["biz_params"]["chipset"], "fixture GPU")
        self.assertNotIn("private-", json.dumps(result))

    def test_live_cases_use_fresh_signature_and_configured_filters(self) -> None:
        """检查所有配置对照组实际发送的字段和报告脱敏；无参数或返回值。"""
        observed = {"user_agent": "private-ua", "snapshot": {"request_url": self.make_url(self.groups),
                                                               "streams": []}}
        with patch("lib.live.LiveInspector") as factory, patch("lib.live.load_session"), \
                patch("lib.live.native_signature", return_value={"ckey": "private-fresh-key",
                      "client_ts": "123", "encryptR1": "private-fresh-r1"}) as signature, \
                patch("lib.live.request_mtop", return_value={"ret": ["SUCCESS"], "data": {}}) as request:
            inspector = factory.return_value.__enter__.return_value
            inspector.evaluate.return_value = observed
            inspector.activated = True
            inspector.closed_owned_inspector = True
            report = live_probe(self.config, 123)
        cases = self.config["live"]["cases"]
        self.assertEqual(request.call_count, len(cases))
        self.assertEqual(signature.call_count, len(cases))
        for case, call in zip(cases, request.call_args_list):
            payload, ua = call.args[3:5]
            groups = {key: json.loads(value) for key, value in payload.items()}
            self.assertEqual(ua, "private-ua")
            self.assertEqual(groups["steal_params"]["ckey"], "private-fresh-key")
            expected_biz = dict(self.groups["biz_params"], encryptR_client="private-fresh-r1")
            for key in case["remove_biz_fields"]:
                expected_biz.pop(key, None)
            self.assertEqual(groups["biz_params"], expected_biz)
            expected_ads = self.groups["ad_params"] if case["ad_fields"] is None else {
                key: self.groups["ad_params"][key] for key in case["ad_fields"] if key in self.groups["ad_params"]}
            self.assertEqual(groups["ad_params"], expected_ads)
        self.assertNotIn("private-", json.dumps(report))
        self.assertFalse(report["independent_file_verified"])

    def test_existing_inspector_remains_running(self) -> None:
        """退出时只断开既有 Inspector 的研究连接；无参数或返回值。"""
        inspector = LiveInspector(self.config, 123)
        connection = inspector.ws = MagicMock()
        with patch.object(inspector, "evaluate") as evaluate:
            inspector.__exit__(None, None, None)
        evaluate.assert_not_called()
        connection.close.assert_called_once()
        self.assertFalse(inspector.closed_owned_inspector)

    def test_owned_inspector_is_closed_even_when_body_fails(self) -> None:
        """上下文发生异常时仍请求回收自己开启的调试端口；无参数或返回值。"""
        inspector = LiveInspector(self.config, 123)
        inspector.activated = True
        connection = inspector.ws = MagicMock()
        with patch.object(inspector, "evaluate") as evaluate, \
                patch.object(inspector, "discover", return_value=[]), patch("lib.live.time.sleep"):
            inspector.__exit__(ValueError, ValueError("fixture"), None)
        self.assertIn("inspector').close()", evaluate.call_args.args[0])
        connection.close.assert_called_once()
        self.assertTrue(inspector.closed_owned_inspector)

    def test_remote_exception_is_sanitized(self) -> None:
        """服务端错误不将原始请求或签名带入异常文字；无参数或返回值。"""
        inspector = LiveInspector(self.config, 123)
        inspector.ws = MagicMock()
        inspector.ws.recv.return_value = json.dumps({"id": 1, "result": {"exceptionDetails": {
            "exception": {"description": "SampleMismatch private-request-key"}}}})
        with self.assertRaisesRegex(ValueError, "SampleMismatch") as raised:
            inspector.evaluate("fixture expression")
        self.assertNotIn("private-", str(raised.exception))

    def test_wrong_process_rejected_before_activation(self) -> None:
        """进程路径不符时不激活调试器；无参数或返回值。"""
        inspector = LiveInspector(self.config, 123)
        with patch("local.youku.inspector.process_executable", return_value=Path("different.exe")), \
                patch("lib.live.subprocess.run") as run:
            with self.assertRaises(ValueError):
                inspector.__enter__()
        run.assert_not_called()
