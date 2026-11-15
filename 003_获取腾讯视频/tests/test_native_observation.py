"""验证客户端协议研究的报告脱敏边界；不附加真实客户端、不访问账号或网络。"""

import json
from pathlib import Path
import unittest
from urllib.parse import urlencode

import yaml

from local.configuration import load_config
from research.observe_native_playback import native_arguments, signing_summary, summarize_request


class NativeObservationTests(unittest.TestCase):
    """仅允许配置的能力字段进入报告，GET 与代理 POST 中的票据原文均不能落盘。"""

    def setUp(self):
        """读取当前研究和播放配置以构造协议参数；无参数、网络操作或返回值。"""
        root = Path(__file__).resolve().parents[1]
        self.research = yaml.safe_load((root / "configs/playback_research.yaml").read_text(encoding="utf-8"))
        self.config = load_config(root / "configs/config.yaml", source="url")
        self.fields = self.research["native_logs"]["query_fields"]
        self.values = {**self.config["playback"]["playback"]["query"], "cKey": "synthetic-private-ckey",
                       "logintoken": json.dumps({"vusession": "synthetic-private-session"})}

    def assert_sanitized(self, summary: dict):
        """检查 summary 仅包含允许的协议参数且不包含合成票据或地址；无返回值。"""
        output = json.dumps(summary)
        self.assertNotIn("synthetic-private-ckey", output)
        self.assertNotIn("synthetic-private-session", output)
        self.assertNotIn("https://", output)
        self.assertLessEqual(set(summary["parameters"]), set(self.fields))
        self.assertEqual(summary["parameters"]["platform"], [str(self.values["platform"])])

    def test_get_request_report_keeps_only_capability_values(self):
        """GET URL 的签名、会话值须移除，同时保留当前配置的能力参数供对照。"""
        summary = summarize_request({"url": "https://vv.video.qq.com/getinfo?" + urlencode(self.values)}, self.fields)
        self.assert_sanitized(summary)

    def test_proxy_body_report_never_keeps_raw_login_json(self):
        """代理 POST 中的 vinfoparam 也执行同一白名单，不能把带票据的正文直接写入报告。"""
        body = json.dumps({"buid": "vinfoad", "vinfoparam": urlencode(self.values)})
        summary = summarize_request({"url": "https://vd.l.qq.com/proxyhttp", "body": body}, self.fields)
        self.assert_sanitized(summary)

    def test_native_clip_arguments_keep_signed_addresses_only_in_memory(self):
        """原生分片参数里的地址与会话值只留内存 URL 列表，结构报告不能包含它们。"""
        address = "https://media.invalid/private-path/file.m3u8?signature=synthetic-private-ckey"
        value = {"urls": [address], "headers": {"Cookie": "synthetic-private-session"},
                 "platform": self.values["platform"]}
        shapes, urls = native_arguments([json.dumps(value)], self.fields)
        self.assertEqual(urls, [address])
        output = json.dumps(shapes)
        self.assertNotIn("synthetic-private-ckey", output)
        self.assertNotIn("synthetic-private-session", output)
        self.assertNotIn("https://", output)

    def test_signing_report_omits_signature_and_device_values(self):
        """签名观察只输出版本、字段长度和已知视频编号，不能写入签名或不透明设备标识。"""
        settings = self.research["native_observation"]["signing"]
        width = settings["frame_widths"][0]
        values = ["4.2", "synthetic-private-ckey"]
        framed = "".join(f"{len(value):0{width}x}{value}" for value in values)
        event = {"strings": [{"index": 0, "value": "synthetic-private-device"}, {"index": 1, "value": "knownVideo"}],
                 "numbers": [], "extra": "synthetic-private-extra", "output": framed, "output_length": len(framed)}
        result = signing_summary(event, settings, {"knownVideo"})
        output = json.dumps(result)
        self.assertNotIn("synthetic-private-ckey", output)
        self.assertNotIn("synthetic-private-device", output)
        self.assertNotIn("synthetic-private-extra", output)
        self.assertEqual(result["strings"][1]["kind"], "selected_video_id")

    def test_paired_length_frame_reports_shape_without_payload(self):
        """原生两长度头必须按调用方布局解析，报告仅保留长度与版本，不写签名段。"""
        settings = self.research["native_observation"]["signing"]
        width = settings["paired_frame_width"]
        values = ["synthetic-private-ckey", "4.2"]
        frame = "".join(f"{len(value):0{width}x}" for value in values) + "".join(values)
        result = signing_summary({"strings": [], "numbers": [], "output": frame}, settings, set())
        paired = next(value for value in result["frames"] if value.get("layout") == "paired_lengths")
        self.assertEqual(paired["lengths"], list(map(len, values)))
        self.assertEqual(paired["version_fields"], [{"index": 1, "value": values[1]}])
        self.assertNotIn(values[0], json.dumps(result))

    def test_malformed_body_field_name_cannot_leak_into_report(self):
        """损坏正文的 JSON 片段不能作为表单字段名称落盘，以免键名携带票据值。"""
        body = urlencode({'{"token":"synthetic-private-session"': "broken"})
        result = summarize_request({"url": "https://media.invalid/proxyhttp", "body": body}, self.fields)
        self.assertEqual(result["unrecognized_name_count"], 1)
        self.assertNotIn("synthetic-private-session", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
