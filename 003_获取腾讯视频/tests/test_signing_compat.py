"""隔离检查候选签名验收的提交边界、范围读取和未知 ABI 拒绝，不访问真实客户端。"""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

from local.configuration import load_config
from local.playback.errors import PlaybackError
from local.playback.hls import Resource
from local.playback.signing_compat import commit_profile, inspect_images, observe_candidate, probe_resource, validate_candidate, verify_links
from research.settings import load_research


class SigningCompatibilityTests(unittest.TestCase):
    """通过阶段替身验证验收完整性；配置和候选值从当前 YAML 派生。"""

    def setUp(self):
        """创建独立签名配置和候选对象；无外部资源读写或返回值。"""
        self.config = load_config(Path(__file__).resolve().parents[1] / "configs/config.yaml", source="url")
        self.settings = self.config["playback"]["playback"]["ckey"]
        self.settings["context"]["mirrors"] = []
        temporary = TemporaryDirectory(prefix="signing_compat_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "signing.yaml"
        self.path.write_text(yaml.safe_dump(self.settings), encoding="utf-8")
        self.original = self.path.read_bytes()
        self.candidate = deepcopy(self.settings)
        self.candidate.update(sha256="a" * 64, client_version="candidate")
        self.candidate["context"].update(sha256="b" * 64, object_rva=self.settings["context"]["object_rva"] + 32)
        self.research, _ = load_research(Path(__file__).resolve().parents[1] / "research/configs/playback_research.yaml")
        self.research["native_observation"]["watch_seconds"] = 1
        self.research["transfer_probe"].update(sample_bytes=16, timeout_seconds=1)
        self.paths = {name: self.root / name for name in (self.settings["module"], self.settings["context"]["module"])}
        for path in self.paths.values():
            path.write_bytes(b"synthetic PE image")

    def stage_patches(self):
        """返回独立的进程、PE、观察及链接验收替身列表；调用方负责启用。"""
        prefix = "local.playback.signing_compat."
        return [patch(prefix + "candidate_images", return_value=(1, self.paths)),
                patch(prefix + "inspect_images", side_effect=lambda *args: (deepcopy(self.candidate), [
                    {"module": self.candidate["context"]["module"], "sha256": self.candidate["context"]["sha256"],
                     "rva": self.candidate["context"]["object_rva"], "size": 32}])),
                patch(prefix + "observe_candidate", return_value={"method": "observed"}),
                patch(prefix + "verify_links", return_value=[{"video_id": "sample"}])]

    def run_validation(self, failure=None):
        """启用隔离阶段后运行验收；failure 可指定抛错阶段及异常，返回报告或转发异常。"""
        from contextlib import ExitStack
        with ExitStack() as stack:
            mocks = [stack.enter_context(value) for value in self.stage_patches()]
            if failure:
                mocks[failure[0]].side_effect = failure[1]
            return validate_candidate(self.config, self.research, self.path, None, Event())

    def test_success_commits_only_after_all_stages(self):
        """真实观察及链接替身均成功时写回允许字段，其他配置保持一致。"""
        report = self.run_validation()
        self.assertTrue(report["profile_updated"])
        self.assertEqual(yaml.safe_load(self.path.read_bytes()), self.candidate)

    def test_observation_and_link_failure_leave_original(self):
        """缺少 ABI 证据或链接验收失败都不更新配置。"""
        for stage in (2, 3):
            with self.subTest(stage=stage), self.assertRaises(PlaybackError):
                self.run_validation((stage, PlaybackError("验收未通过")))
            self.assertEqual(self.path.read_bytes(), self.original)

    def test_verified_device_mirrors_are_committed_together(self):
        """同值设备副本与主位置一起提交，摘要和位置均保留用于正式签名核对。"""
        context = self.candidate["context"]
        context["mirrors"] = [{key: context[key] for key in ("module", "sha256", "object_rva", "guid_offset")}]
        context["mirrors"][0]["object_rva"] += 64
        report = self.run_validation()
        self.assertTrue(report["profile_updated"])
        self.assertEqual(yaml.safe_load(self.path.read_bytes()), self.candidate)

    def test_changed_dll_is_rejected_before_commit(self):
        """候选通过后 DLL 再变化须拒绝提交，原配置保持不变。"""
        changed = deepcopy(self.candidate)
        changed["sha256"] = "c" * 64
        patches = self.stage_patches()
        from contextlib import ExitStack
        with ExitStack() as stack:
            mocks = [stack.enter_context(value) for value in patches]
            mocks[1].side_effect = [(deepcopy(self.candidate), []), (changed, [])]
            with self.assertRaisesRegex(PlaybackError, "DLL 或进程已变化"):
                validate_candidate(self.config, self.research, self.path, None, Event())
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_external_edit_is_preserved(self):
        """用户在验收期间编辑 YAML 时保留外部编辑。"""
        changed = self.original + b"# external edit\n"
        self.path.write_bytes(changed)
        with self.assertRaisesRegex(PlaybackError, "验收期间被修改"):
            commit_profile(self.path, self.original, self.candidate)
        self.assertEqual(self.path.read_bytes(), changed)

    def test_comments_bom_and_crlf_survive_commit(self):
        """提交仅替换允许标量，保留 BOM、CRLF、注释及不相关字段。"""
        original = b"\xef\xbb\xbf# user comment\r\n" + self.original.replace(b"\n", b"\r\n")
        self.path.write_bytes(original)
        commit_profile(self.path, original, self.candidate)
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf# user comment\r\n"))
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertEqual(yaml.safe_load(raw.decode("utf-8-sig")), self.candidate)

    def test_cancel_before_commit_preserves_original(self):
        """观察结束后取消时不进入授权检查或提交。"""
        from contextlib import ExitStack
        from local.runtime import TaskCancelled
        cancel = Event()
        with ExitStack() as stack:
            mocks = [stack.enter_context(value) for value in self.stage_patches()]
            mocks[2].side_effect = lambda *args, **kwargs: cancel.set()
            with self.assertRaises(TaskCancelled):
                validate_candidate(self.config, self.research, self.path, None, cancel)
            mocks[3].assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_wrong_architecture_never_reaches_observation(self):
        """非 x64 候选在静态检查时拒绝，不推断其他平台 ABI。"""
        image = Mock()
        image.__enter__ = Mock(return_value=image)
        image.__exit__ = Mock(return_value=False)
        image.FILE_HEADER.Machine = 0x14C
        with patch("local.playback.signing_compat.pefile.PE", return_value=image), self.assertRaisesRegex(PlaybackError, "x64"):
            inspect_images(self.settings, self.paths)

    def test_failed_observer_detaches_without_forwarding_raw_error(self):
        """脚本异常只返回固定错误并分离会话，原始数据不进入异常。"""
        script, session = Mock(), Mock()
        session.create_script.return_value = script
        def subscribe(name, callback):
            """给 name 的 callback 传递合成异常；返回 None。"""
            callback({"type": "error", "description": "private-ticket"}, None)
        script.on.side_effect = subscribe
        with patch("local.playback.signing_compat.frida.attach", return_value=session), self.assertRaises(PlaybackError) as caught:
            observe_candidate(1, self.candidate, [], 1, Event(), observation={})
        self.assertNotIn("private-ticket", str(caught.exception))
        script.unload.assert_called_once()
        session.detach.assert_called_once()

    def test_byte_range_and_short_read(self):
        """范围资源按原始偏移读取；返回不足要求字节数不能通过候选验收。"""
        resource = Resource("https://example.invalid/sample", (100, 16), 1)
        response, transport = Mock(), Mock()
        response.code, response.headers = 206, {"Content-Range": "bytes 100-115/200"}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        transport.open.return_value = response
        response.read1.side_effect = [b"x" * 16]
        self.assertEqual(probe_resource(transport, resource, self.research["transfer_probe"])["bytes"], 16)
        transport.open.assert_called_once_with(resource.url, {"Range": "bytes=100-115"})
        response.read1.side_effect = [b"short", b""]
        with self.assertRaisesRegex(PlaybackError, "不足"):
            probe_resource(transport, resource, self.research["transfer_probe"])

    def test_empty_links_cannot_pass(self):
        """无实际验收条目时不能将空循环判为成功。"""
        with patch("local.playback.tasks.requests", return_value=[]), patch("local.playback.client.NativeClient"):
            with self.assertRaisesRegex(PlaybackError, "没有实际验证"):
                verify_links(self.config, self.research, 1)


if __name__ == "__main__":
    unittest.main()
