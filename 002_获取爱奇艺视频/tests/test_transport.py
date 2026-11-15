"""覆盖实际解码中发现的 EBSP 重复转义及 TS 包重建边界。"""

from pathlib import Path
import tempfile
import unittest

from qsv_local.native import NativeV5
from qsv_local.transport import TransportDecoder, payload_offset, service_descriptor


class StubSdk:
    """仅用于隔离传输层测试；返回预先指定的 SDK 明文，不模拟加解密算法。"""

    def __init__(self, clear: bytes):
        """保存样例；clear 为包含已有防竞争字节的 EBSP；无返回值。"""
        self.clear = clear
        self.inputs = []

    def decrypt(self, data: bytes) -> bytes:
        """记录输入并返回样例；data 为移除外层转义的加密字节；返回预设 EBSP。"""
        self.inputs.append(data)
        return self.clear

    def configure(self, descriptor: bytes) -> None:
        """接受测试 descriptor 描述符并保存最近一次原文；无返回值，不实现授权或密钥处理。"""
        self.descriptor = descriptor


class TransportTests(unittest.TestCase):
    """验证媒体格式边界，不依赖用户手动修改的路径、剧集或 SDK 配置值。"""

    def test_sdk_ebsp_is_preserved_without_double_escaping(self):
        """重现曾导致 SPS 损坏的重复转义；无参数，无返回值。"""
        clear = bytes.fromhex("01016000000300900000030000030096")
        sdk = StubSdk(clear)
        decoder = TransportDecoder(sdk, 1024)
        decoder.stream_type = 0x24
        # PES 带 5 字节 PTS；测试解密后原始时间戳仍逐字节一致。
        header = bytes.fromhex("000001e000008080052100010001")
        start = b"\0\0\0\1"
        nal_header = b"\x42\x01"
        encrypted = b"\0\0\3\1cipher-crc"
        result = decoder._decrypt_pes(header + start + nal_header + encrypted)
        self.assertEqual(result, header + start + nal_header + clear)
        self.assertEqual(sdk.inputs, [b"\0\0\1cipher-crc"])

    def test_shorter_payload_preserves_pcr_and_packet_size(self):
        """负载缩短时保留 PCR，新增填充进入适配字段；无参数，无返回值。"""
        decoder = TransportDecoder(StubSdk(b""), 1024)
        pcr = bytes.fromhex("105234abcd007e")
        original = bytes([0x47, 0x41, 0x00, 0x30, len(pcr)]) + pcr
        original += b"x" * (188 - len(original))
        payload = b"original-pts-and-video"
        result = decoder._packet(original, payload, True)
        self.assertEqual(len(result), 188)
        self.assertEqual(result[5:5 + len(pcr)], pcr)
        self.assertEqual(result[payload_offset(result):], payload)
        self.assertTrue(result[1] & 0x40)

    def test_adaptation_only_packet_keeps_continuity(self):
        """无负载包不增加连续计数且不保留 PUSI；无参数，无返回值。"""
        decoder = TransportDecoder(StubSdk(b""), 1024)
        decoder.continuity = 6
        packet = decoder._packet(bytes([0x47, 0x41, 0x00, 0x10]), b"", False)
        self.assertEqual(len(packet), 188)
        self.assertEqual(packet[3], 0x26)
        self.assertFalse(packet[1] & 0x40)
        self.assertEqual(payload_offset(packet), 188)

    def test_changed_client_binary_is_rejected_before_loading(self):
        """DLL 不匹配时必须在调用任何内部地址前失败；无参数，无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "different.dll"
            path.write_bytes(b"a different client build")
            with self.assertRaisesRegex(ValueError, "DLL"):
                NativeV5({"dll_path": str(path), "dll_sha256": "0" * 64}, b"")

    def test_clear_ts_is_processed_without_calling_decrypt(self):
        """合法明文 TS 按原内容处理并记录 clear 模式，仍须由正式工作流做完整解码；无参数或返回值。"""
        def packet(pid: int, payload: bytes) -> bytes:
            """将 pid 和短 payload 封装为带 PUSI 的完整 TS 包；返回填充后的 188 字节。"""
            return bytes([0x47, 0x40 | (pid >> 8), pid & 255, 0x10]) + payload.ljust(184, b"\xff")

        pat = b"\0" + bytes.fromhex("00b00d0001c100000001e10000000000")
        pmt = b"\0" + bytes.fromhex("02b0120001c10000e101f00024e101f00000000000")
        pes = bytes.fromhex("000001e000008000000000010201") + b"clear-video"
        sdk = StubSdk(b"")
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "plain.ts", Path(directory) / "result.ts"
            source.write_bytes(packet(0, pat) + packet(0x100, pmt) + packet(0x101, pes))
            stats = TransportDecoder(sdk, source.stat().st_size).convert(source, target)
            self.assertEqual(stats["protection"], "clear")
            self.assertEqual(stats["plain_pes"], 1)
            self.assertEqual(stats["video_pes"], 1)
            self.assertEqual(sdk.inputs, [])
            self.assertTrue(target.exists())

    def test_transport_scrambling_is_explicitly_rejected(self):
        """未知 TS 包级加密不能被当作明文成功处理；无参数或返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "scrambled.ts", Path(directory) / "result.ts"
            source.write_bytes(bytes([0x47, 0x40, 0, 0x90]) + b"\0" * 184)
            with self.assertRaisesRegex(ValueError, "包级加密"):
                TransportDecoder(StubSdk(b""), 1024).convert(source, target)

    def test_both_mdcm_descriptor_styles_are_recognized_without_changing_fields(self):
        """不同视频的 mdcm 前缀与中间标记均原样传递，普通服务名保持明文；无参数或返回值。"""
        for name in (b"sample|mdcm|gv|fixture|", b"mdcm|fixture|aa|opaque-data|bb|sample1|", b"clear"):
            with self.subTest(name=name):
                data = b"\x01\0" + bytes([len(name)]) + name
                descriptor = b"\x48" + bytes([len(data)]) + data
                service = b"\0\x01\xfc" + (0x8000 | len(descriptor)).to_bytes(2, "big") + descriptor
                section = bytearray(bytes.fromhex("42b0000001c100000001ff") + service + b"\0" * 4)
                length = len(section) - 3
                section[1], section[2] = 0xB0 | (length >> 8), length & 255
                packet = (bytes([0x47, 0x40, 0x11, 0x10, 0]) + section).ljust(188, b"\xff")
                self.assertEqual(service_descriptor(packet), None if name == b"clear" else name)


if __name__ == "__main__":
    unittest.main()
