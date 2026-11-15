"""隔离验证原生签名进程识别、ABI 拒绝和会话回收，不附加真实进程、不读取账号。"""

from copy import deepcopy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from local.configuration import load_config
from local.playback.client import NativeClient
from local.playback.errors import PlaybackError
from local.playback.native_sign import generate, verified_process


class NativeSignTests(unittest.TestCase):
    """未知 DLL 和多个播放器须拒绝，签名失败不能泄露原文或遗留附加会话。"""

    def setUp(self):
        """从当前配置构造独立临时 SDK 和合成进程，不依赖用户实际客户端版本或账号；无返回值。"""
        self.config = deepcopy(load_config(Path(__file__).resolve().parents[1] / "configs/config.yaml", source="url"))
        self.settings = self.config["playback"]["playback"]["ckey"]
        folder = TemporaryDirectory(prefix="native_sign_")
        self.addCleanup(folder.cleanup)
        # Windows 的临时目录可能返回 8.3 短名；模拟配置加载器使用的已解析绝对路径。
        root = Path(folder.name).resolve()
        self.config["playback"]["native"]["dll_root"] = root
        library = root / self.settings["module"]
        library.write_bytes(b"synthetic-sdk")
        self.settings["sha256"] = hashlib.sha256(library.read_bytes()).hexdigest()
        context = root / self.settings["context"]["module"]
        context.write_bytes(b"synthetic-context")
        self.settings["context"]["sha256"] = hashlib.sha256(context.read_bytes()).hexdigest()
        self.process = SimpleNamespace(pid=1, name=lambda: self.settings["process_names"][0],
            exe=lambda: str(root / self.settings["process_names"][0]),
            memory_maps=lambda **_: [SimpleNamespace(path=str(value)) for value in (library, context)])

    def test_unique_process_and_unknown_dll(self):
        """按当前名称和安装根核对唯一进程；摘要变化必须拒绝，不能自动接受另一个 SDK。"""
        with patch("local.playback.native_sign.psutil.process_iter", return_value=[self.process]):
            self.assertEqual(verified_process(self.config, self.settings), self.process.pid)
            self.settings["sha256"] = hashlib.sha256(b"different-sdk").hexdigest()
            with self.assertRaises(PlaybackError):
                verified_process(self.config, self.settings)

    def test_ambiguous_process_is_rejected(self):
        """两个符合条件的播放器不得任意选取，以免使用错误的 SDK 上下文。"""
        with patch("local.playback.native_sign.psutil.process_iter", return_value=[self.process, self.process]):
            with self.assertRaises(PlaybackError):
                verified_process(self.config, self.settings)

    def test_rpc_failure_is_sanitized_and_detached(self):
        """SDK 异常不转发票据原文，失败后必须卸载脚本并分离会话。"""
        session, script = Mock(), Mock()
        session.create_script.return_value = script
        script.exports_sync.generate.side_effect = RuntimeError("synthetic-private-token")
        with patch("local.playback.native_sign.verified_process", return_value=1), patch("local.playback.native_sign.frida.attach", return_value=session):
            with self.assertRaises(PlaybackError) as caught:
                generate(self.config, {})
        self.assertNotIn("synthetic-private-token", str(caught.exception))
        script.unload.assert_called_once()
        session.detach.assert_called_once()

    def test_sdk_error_code_never_becomes_a_signature(self):
        """SDK 的负错误码不能当成成功签名继续请求播放接口；会话仍须回收。"""
        session, script = Mock(), Mock()
        session.create_script.return_value = script
        script.exports_sync.generate.return_value = {"ckey": "-1", "guid": "synthetic-guid"}
        with patch("local.playback.native_sign.verified_process", return_value=1), patch("local.playback.native_sign.frida.attach", return_value=session):
            with self.assertRaises(PlaybackError):
                generate(self.config, {})
        script.unload.assert_called_once()
        session.detach.assert_called_once()

    def test_request_uses_the_same_device_guid_as_sdk_signature(self):
        """播放参数必须使用 SDK 返回的设备 GUID，不能用随机 GUID 造成接口拒绝。"""
        playback = self.config["playback"]["playback"]
        tokens = {name: "synthetic-account" for name in self.config["playback"]["native"]["token_fields"]}
        client = NativeClient(self.config, user={"tokens": tokens, "app_version": "synthetic"})
        signed = {"ckey": "synthetic-private-signature", "guid": "synthetic-private-device"}
        with patch("local.playback.client.native_key", return_value=signed):
            query, _ = client.signed_query("cover", "video", playback["query"])
        self.assertEqual(query["guid"], signed["guid"])
        self.assertEqual(query["cKey"], signed["ckey"])


if __name__ == "__main__":
    unittest.main()
