"""验证媒体采样的来源、边界、分段结构与脱敏；全部使用虚拟资源，无真实网络或客户端。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.request import Request

from lib.media import MediaRedirect, fetch_media, parse_manifest, require_media_url, transport_summary, verify_media
from lib.probe import load_config


class MediaTests(unittest.TestCase):
    """覆盖清单、请求限制和本地验证状态；无构造参数，结果由 unittest 报告。"""

    def setUp(self) -> None:
        """按当前配置建立隔离上下文；无参数或返回值。"""
        self.config = load_config(Path(__file__).resolve().parents[1] / "configs/research.yaml")
        self.options = self.config["media"]
        self.base = self.options["allowed_schemes"][0] + "://" + self.options["allowed_domains"][0] + "/playlist/a.m3u8"

    def test_private_markers_and_relative_segments(self) -> None:
        """保留分段时长、私有明文标记与不连续点，不凭缺少标准 KEY 推断无保护；无参数或返回值。"""
        raw = b"#EXTM3U\n#EXTINF:1.5,\n#EXT-X-PRIVINF:FILESIZE=188,DRM_NOTENCRYPT=0\na.ts?secret=x\n#EXT-X-DISCONTINUITY\n#EXTINF:2,\n#EXT-X-PRIVINF:DRM_NOTENCRYPT=1\nb.ts\n#EXT-X-ENDLIST\n"
        result = parse_manifest(raw, self.base)
        self.assertEqual(result["duration"], 3.5)
        self.assertEqual(result["key_methods"], [])
        self.assertEqual(result["segments"][0]["private"]["DRM_NOTENCRYPT"], 0)
        self.assertEqual(result["segments"][1]["discontinuity"], 1)
        self.assertIn("/playlist/a.ts?secret=x", result["segments"][0]["url"])

    def test_incomplete_and_unsupported_manifest_rejected(self) -> None:
        """拒绝缺时长、无结束、范围、主清单与非法数字，避免误下载结构；无参数或返回值。"""
        for content in ("#EXTM3U\na.ts\n#EXT-X-ENDLIST", "#EXTM3U\n#EXTINF:1,\na.ts",
                        "#EXTM3U\n#EXTINF:nan,\na.ts\n#EXT-X-ENDLIST",
                        "#EXTM3U\n#EXT-X-BYTERANGE:188@0\n#EXT-X-ENDLIST",
                        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n#EXT-X-ENDLIST",
                        "#EXTM3U\n#EXTINF:1,\n#EXTINF:2,\na.ts\n#EXT-X-ENDLIST"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_manifest(content.encode(), self.base)

    def test_media_domain_and_redirect_rejected(self) -> None:
        """初始请求与重定向均拒绝域名伪装及 URL 凭据；无参数或返回值。"""
        domains, schemes = self.options["allowed_domains"], self.options["allowed_schemes"]
        require_media_url(self.base, domains, schemes)
        for url in ("https://" + domains[0] + ".invalid/a", "file:///private", "https://secret@" + domains[0] + "/a"):
            with self.assertRaises(ValueError):
                require_media_url(url, domains, schemes)
            with self.assertRaises(ValueError):
                MediaRedirect(domains, schemes).redirect_request(Request(self.base), None, 302, "", {}, url)

    def test_length_budget_and_truncation(self) -> None:
        """预算、HTTP 长度不一致时失败，不输出临时地址；无参数或返回值。"""
        response = MagicMock(status=200, url=self.base)
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        with patch("local.youku.manifest.build_opener", return_value=opener):
            for header, data, limit in (("9", b"", 8), ("8", b"short", 8), (None, b"123456789", 8)):
                response.headers.get.return_value = header
                response.read.return_value = data
                with self.assertRaises(ValueError):
                    fetch_media(self.options, self.base, limit)
            response.headers.get.return_value = "3"
            response.read.return_value = b"abc"
            data, _, _ = fetch_media(self.options, self.base, 8)
            self.assertEqual(data, b"abc")
            self.assertNotIn("Cookie", opener.open.call_args.args[0].headers)

    def test_transport_alignment_does_not_claim_clear_video(self) -> None:
        """TS 包结构正常不产生解码成功标记，截断或错同步明确显示失败；无参数或返回值。"""
        packet = bytes([0x47, 0, 0, 0x10]) + bytes(184)
        result = transport_summary(packet * 2)
        self.assertTrue(result["ts_aligned"])
        self.assertNotIn("decode_passed", result)
        self.assertFalse(transport_summary(packet[:-1])["ts_aligned"])
        self.assertFalse(transport_summary(bytes(188))["ts_aligned"])

    def test_decode_failure_and_no_full_file_claim(self) -> None:
        """成功抽样不等于全片，失败诊断脱敏，篡改样本拒绝执行 FFmpeg；无参数或返回值。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            work = root / "samples"
            work.mkdir()
            data = bytes([0x47, 0, 0, 0x10]) + bytes(184)
            (work / "fixture.ts").write_bytes(data)
            report = {"sample": self.config["sample"], "stream": {"stream_type": self.options["stream_type"]},
                      "work_subdir": "samples", "samples": [{"file": "fixture.ts", "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()}]}
            input_path = root / "report.json"
            input_path.write_text(json.dumps(report), encoding="utf-8")
            config = dict(self.config, media=dict(self.options, work_dir=root))
            with patch("lib.media.subprocess.run", return_value=SimpleNamespace(returncode=1,
                       stderr=b"Invalid data at https://private.invalid/?secret=value", stdout=b"")) as run:
                result = verify_media(config, input_path)
                self.assertFalse(result["all_samples_decoded"])
                self.assertNotIn("secret", json.dumps(result))
                run.return_value = SimpleNamespace(returncode=0, stderr=b"", stdout=b"0,0,0,1,188,hash\n1,0,0,1,188,hash\n")
                result = verify_media(config, input_path)
                self.assertTrue(result["all_samples_decoded"])
                self.assertFalse(result["independent_file_verified"])
                run.return_value = SimpleNamespace(returncode=0, stderr=b"", stdout=b"")
                self.assertFalse(verify_media(config, input_path)["all_samples_decoded"])
                (work / "fixture.ts").write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "HashMismatch"):
                    verify_media(config, input_path)
                self.assertEqual(run.call_count, 3)
