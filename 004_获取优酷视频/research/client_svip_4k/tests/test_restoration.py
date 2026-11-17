"""媒体还原的 TS/PES 边界、来源绑定、版本拒绝和脱敏测试；不连接真实客户端。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Crypto.Cipher import AES

from lib.key_bridge import derive_key
from lib.probe import load_config
from lib.restoration import restore_samples
from lib.transport import decrypt_transport


def fixture_transport(key: bytes) -> tuple[bytes, bytes]:
    """构造含跨包 PES、非整块尾部和自适应区的音视频 TS；key 为 16 字节虚拟密钥；返回密文和预期明文。"""
    encrypted, clear = bytearray(), bytearray()
    cipher = AES.new(key, AES.MODE_ECB)
    for pid, stream in ((256, 0xE0), (257, 0xC0)):
        payload = bytes(range(251)) * 2 + b"tail"
        aligned = len(payload) // 16 * 16
        encoded = cipher.encrypt(payload[:aligned]) + payload[aligned:]
        header = b"\0\0\1" + bytes([stream]) + (len(payload) + 3).to_bytes(2, "big") + b"\x80\0\0"
        packets = (header + encoded, header + payload)
        for destination, data in zip((encrypted, clear), packets):
            for index, offset in enumerate(range(0, len(data), 160)):
                chunk = data[offset:offset + 160]
                adaptation_length = 183 - len(chunk)
                ts_header = bytes([0x47, ((pid >> 8) & 0x1F) | (0x40 if index == 0 else 0), pid & 255, 0x30 | index])
                adaptation = bytes([adaptation_length, 0]) + bytes([255]) * (adaptation_length - 1)
                destination.extend(ts_header + adaptation + chunk)
    return bytes(encrypted), bytes(clear)


class RestorationTests(unittest.TestCase):
    """验证还原方法的数据边界及证据绑定；无构造参数，结果交由 unittest 报告。"""

    def setUp(self) -> None:
        """从当前用户配置构造测试上下文；无参数或返回值。"""
        self.config = load_config(Path(__file__).resolve().parents[1] / "configs/research.yaml")
        self.key = bytes(range(16))

    def test_pes_payload_only_and_tail_preserved(self) -> None:
        """跨 TS 包还原负载，精确保持所有头、自适应区和非整块尾部；无参数或返回值。"""
        encrypted, expected = fixture_transport(self.key)
        actual, stats = decrypt_transport(encrypted, self.key)
        self.assertEqual(actual, expected)
        self.assertEqual(stats["video_pes"], 1)
        self.assertEqual(stats["audio_pes"], 1)
        self.assertEqual(len(actual), len(encrypted))

    def test_truncation_continuity_and_invalid_key_rejected(self) -> None:
        """拒绝截断、丢包连续号和错误密钥长度；无参数或返回值。"""
        encrypted, _ = fixture_transport(self.key)
        with self.assertRaises(ValueError):
            decrypt_transport(encrypted[:-1], self.key)
        bad = bytearray(encrypted)
        bad[188 + 3] = (bad[188 + 3] & 0xF0) | 5
        with self.assertRaisesRegex(ValueError, "ContinuityGap"):
            decrypt_transport(bytes(bad), self.key)
        with self.assertRaises(ValueError):
            decrypt_transport(encrypted, b"short")

    def test_library_version_rejected_before_load(self) -> None:
        """摘要不符时禁止调用 DLL，避免将旧偏移用于新版本；无参数或返回值。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.dll"
            path.write_bytes(b"unrecognized library")
            with patch("lib.key_bridge.ctypes.WinDLL") as load, self.assertRaisesRegex(ValueError, "VersionMismatch"):
                derive_key({"library": str(path), "sha256": self.config["restoration"]["library_sha256"]})
            load.assert_not_called()

    def test_source_hash_and_stream_binding_and_redaction(self) -> None:
        """检查当前来源、输入摘要及候选报告脱敏，输入不符时不调用授权处理；无参数或返回值。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            work = root / "fixture"
            work.mkdir()
            data, expected = fixture_transport(self.key)
            path = work / "encrypted.ts"
            path.write_bytes(data)
            stream = {"stream_type": self.config["media"]["stream_type"], "width": 320, "height": 240,
                      "size": len(data), "audio_lang": "fixture", "drm_type": "copyrightDRM"}
            report = {"sample": self.config["sample"], "stream": stream, "work_subdir": "fixture", "samples": [
                {"file": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "index": 0, "duration": 1}]}
            input_path = root / "media.json"
            input_path.write_text(json.dumps(report), encoding="utf-8")
            config = dict(self.config, media=dict(self.config["media"], work_dir=root))
            material = {"stream": stream, "client": "secret-client", "server": "secret-server", "copyright": "secret-license"}
            with patch("lib.restoration.current_material", return_value=(material, {})), \
                    patch("lib.restoration.session_key", return_value=self.key) as derive:
                result = restore_samples(config, 123, input_path)
                self.assertEqual((work / result["samples"][0]["file"]).read_bytes(), expected)
                self.assertNotIn("secret-", json.dumps(result))
                self.assertFalse(result["independent_file_verified"])
                path.write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "HashMismatch"):
                    restore_samples(config, 123, input_path)
                self.assertEqual(derive.call_count, 1)
                material["stream"] = dict(stream, width=640)
                with self.assertRaisesRegex(ValueError, "StreamMismatch"):
                    restore_samples(config, 123, input_path)
