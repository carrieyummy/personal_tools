"""隔离验证内嵌 MPD、调度字面量和 CENC 结构边界，不连接账号或公网。"""

from pathlib import Path
import struct
import unittest
from unittest.mock import Mock

import yaml

from qsv_local.playback.manifests import parse_media_manifest, ranged_url
from qsv_local.playback.media import dispatch_address
from qsv_local.playback.mp4 import fragment, initialization


def box(kind: bytes, body: bytes) -> bytes:
    """构造类型 kind 和正文 body 的合成 MP4 box；返回网络字节序数据。"""
    return struct.pack(">I4s", len(body) + 8, kind) + body


class ManifestTests(unittest.TestCase):
    """核对内容类型、范围及不执行调度脚本的约束。"""

    def setUp(self):
        """从当前配置读取媒体域名，构造无真实签名的测试目标；无返回值。"""
        self.profile = yaml.safe_load((Path(__file__).resolve().parents[1] / "configs/playback.yaml").read_text(encoding="utf-8"))
        self.host = "synthetic" + self.profile["playback"]["resource_host_suffix"]

    def test_mpd_in_m3u8_field_is_recognized_by_content(self):
        """内嵌 XML 不能按字段名当 HLS，且闭区间必须正确转换；无返回值。"""
        body = f'''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT1M2.5S"><Period><AdaptationSet contentType="video"><Representation codecs="av01.0.12M.08" mimeType="video/mp4"><SegmentList><Initialization sourceURL="https://{self.host}/init.mp4" range="0-9"/><SegmentURL media="https://{self.host}/part.mp4" mediaRange="0-19"/></SegmentList></Representation></AdaptationSet></Period></MPD>'''.encode()
        result = parse_media_manifest(body, {"m3u8": body.decode()}, self.profile)
        self.assertEqual(result["format"], "dash")
        self.assertEqual(result["duration"], 62.5)
        self.assertTrue(result["segments"][0]["url"].endswith("start=0&end=10"))
        self.assertEqual(result["tracks"][0]["codecs"], "av01.0.12M.08")

    def test_conflicting_dash_ranges_are_rejected(self):
        """地址与声明范围冲突时不猜测优先级；无返回值。"""
        with self.assertRaisesRegex(ValueError, "冲突"):
            ranged_url("https://" + self.host + "/part.mp4?start=0&end=10", "0-10")

    def test_native_var_dispatch_is_parsed_without_execution(self):
        """原生对象字面量可提取唯一地址，签名中的逗号或冒号不能被当属性名改写；无返回值。"""
        url = "https://" + self.host + "/part.mp4?auth=a,b:c"
        body = ('var videoUrl={code:"A00000",data:{"l":"' + url + '","t":"4"}};').encode()
        self.assertEqual(dispatch_address(body), url)
        with self.assertRaises((RuntimeError, ValueError)):
            dispatch_address(b'var videoUrl={code:run_code(),data:{}};')

    def test_wrong_native_dispatch_status_is_rejected(self):
        """调度拒绝不能变成媒体分片；无返回值。"""
        with self.assertRaisesRegex(RuntimeError, "调度失败"):
            dispatch_address(b'var videoUrl={code:"A00500",data:{}};')


class CencStructureTests(unittest.TestCase):
    """以合成轨道核对 SDK 调用范围和保护标记，不模拟真实密码算法。"""

    def make_initialization(self, iv_size=8):
        """构造 IV 长度 iv_size 的单轨 AV1 初始化；返回合成字节。"""
        tkhd = box(b"tkhd", b"\0" * 12 + struct.pack(">I", 1) + b"\0" * 8)
        tenc = box(b"tenc", b"\0" * 6 + bytes([1, iv_size]) + b"k" * 16)
        sinf = box(b"sinf", box(b"frma", b"av01") + box(b"schi", tenc))
        entry = box(b"encv", b"\0" * 78 + sinf)
        stsd = box(b"stsd", b"\0" * 4 + struct.pack(">I", 1) + entry)
        return box(b"moov", box(b"trak", tkhd + box(b"mdia", box(b"minf", box(b"stbl", stsd)))))

    def test_initialization_and_sample_use_verified_cenc_boundaries(self):
        """验证初始化恢复原编码，且仅将 senc 声明的样本、IV及子样本表交给 SDK；无返回值。"""
        clear_init, tracks = initialization(self.make_initialization())
        self.assertEqual(tracks[1]["descriptions"][1]["iv_size"], 8)
        self.assertIn(b"av01", clear_init)
        self.assertNotIn(b"encv", clear_init)
        tfhd = box(b"tfhd", b"\0\x02\0\0" + struct.pack(">I", 1))
        table = struct.pack(">HI", 1, 2)
        senc = box(b"senc", b"\0\0\0\2" + struct.pack(">I", 1) + b"i" * 8 + struct.pack(">H", 1) + table)
        trun = box(b"trun", b"\0\0\2\1" + struct.pack(">IiI", 1, 0, 3))
        moof = box(b"moof", box(b"traf", tfhd + trun + senc))
        trun = box(b"trun", b"\0\0\2\1" + struct.pack(">IiI", 1, len(moof) + 8, 3))
        moof = box(b"moof", box(b"traf", tfhd + trun + senc))
        sdk = Mock()
        sdk.decrypt_cenc.return_value = b"abc"
        result, proof = fragment(moof + box(b"mdat", b"xyz"), sdk, tracks)
        sdk.decrypt_cenc.assert_called_once_with(b"xyz", b"i" * 8, table)
        self.assertTrue(result.endswith(b"abc"))
        self.assertEqual(proof, {"samples": 1, "encrypted_samples": 1})

    def test_unverified_iv_size_is_not_passed_to_native_sdk(self):
        """未核验的 IV 布局在本机调用前被拒绝；无返回值。"""
        with self.assertRaisesRegex(ValueError, "8 字节"):
            initialization(self.make_initialization(16))


if __name__ == "__main__":
    unittest.main()
