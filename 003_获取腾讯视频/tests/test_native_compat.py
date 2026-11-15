"""验证 DLL 自动验收的指纹提交、失败隔离和用户配置保护。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

import yaml

from local.batch import Batch
from local.configuration import load_config
from local.native_compat import profile_lock, update_digest
from local.native import NativeFingerprintMismatch
from local.process import run_process
from local.runtime import TaskCancelled
from local.storage import atomic_json
from tests.test_batch import MemoryJournal


class NativeCompatibilityTests(unittest.TestCase):
    """使用独立 DLL 文件和阶段替身验证自动验收，不加载测试 DLL。"""

    def setUp(self):
        """基于现有配置创建隔离配置、候选 DLL 和样本元数据，无返回值。"""
        self.temporary = tempfile.TemporaryDirectory(prefix="tencent_native_compat_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = deepcopy(load_config(Path("configs/config.yaml"), source="download"))
        profile = deepcopy(self.config["native"])
        profile["dll_root"] = self.root / "client"
        folder = profile["dll_root"] / "1.0"
        folder.mkdir(parents=True)
        self.dll = folder / profile["dll_name"]
        self.dll.write_bytes(b"candidate dll")
        self.new_digest = hashlib.sha256(self.dll.read_bytes()).hexdigest()
        self.old_digest = hashlib.sha256(b"previous dll").hexdigest()
        profile["dll_sha256"] = self.old_digest
        self.config["native"] = profile
        self.config["native_profile"] = self.root / "native.yaml"
        self.config["work_dir"] = self.root / "work"
        serializable = {**profile, "dll_root": str(profile["dll_root"])}
        self.config["native_profile"].write_text("# 用户注释\n" + yaml.safe_dump(serializable, allow_unicode=True), encoding="utf-8")
        self.original = self.config["native_profile"].read_bytes()
        self.task = {"video_id": "sample", "title": "sample", "folder": self.root / "source"}
        self.state = {"segments": [{"path": self.root / "source.ts", "size": 206, "media_bytes": 188}],
                      "duration": 1.0, "sources": [{"path": "sample", "size": 206, "sha256": "sample"}],
                      "playlist_sha256": "playlist"}
        self.journal = MemoryJournal()
        self.batch = Batch(self.config, self.journal)

    def fake_process(self, command, log_path, config, *args, **kwargs):
        """模拟隔离进程，核对候选指纹并生成摘要与 TS；command 最后一项为请求路径。"""
        request = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        self.assertEqual(request["config"]["native"]["dll_sha256"], self.new_digest)
        Path(request["target"]).write_bytes(b"media")
        atomic_json(Path(request["result"]), {"sources": self.state["sources"]})

    def fake_mux(self, source, work, entry):
        """模拟 source 的封装并返回 work 中的候选路径；entry 是日志项。"""
        candidate = work / "candidate.partial.mp4"
        candidate.write_bytes(b"candidate")
        return candidate

    def test_success_updates_only_digest_after_complete_validation(self):
        """候选还原、封装、完整解码和来源复核均成功后，仅替换指纹。"""
        with patch("local.batch.source_state", return_value=self.state) as source, \
             patch("local.batch.run_process", side_effect=self.fake_process), \
             patch.object(self.batch, "mux_candidate", side_effect=self.fake_mux), \
             patch.object(self.batch, "media", return_value={}), \
             patch.object(self.batch, "verify", return_value={}) as verify:
            self.batch.ensure_native_compatibility([self.task])
        self.assertEqual(source.call_count, 2)
        verify.assert_called_once()
        self.assertEqual(self.config["native_profile"].read_bytes(),
                         self.original.replace(self.old_digest.encode(), self.new_digest.encode()))
        self.assertEqual(self.config["native"]["dll_sha256"], self.new_digest)
        self.assertEqual(self.journal.entries[-1]["status"], "completed")

    def test_failed_child_keeps_original_configuration(self):
        """子进程异常退出时不修改指纹，并记录验收失败。"""
        with patch("local.batch.source_state", return_value=self.state), \
             patch("local.batch.run_process", side_effect=RuntimeError("子进程异常退出")):
            with self.assertRaisesRegex(RuntimeError, "子进程异常退出"):
                self.batch.ensure_native_compatibility([self.task])
        self.assertEqual(self.config["native_profile"].read_bytes(), self.original)
        self.assertEqual(self.journal.entries[-1]["status"], "failed")

    def test_real_child_abnormal_exit_does_not_update(self):
        """真实子进程异常退出由父进程捕获，且不更新原配置。"""
        def crash(command, log_path, config, *args, **kwargs):
            """启动立即异常退出的测试进程；command 等参数与原接口相同。"""
            run_process([sys.executable, "-c", "import os; os._exit(23)"], log_path, config,
                        cancel_event=kwargs.get("cancel_event"), label="DLL 验证子进程")

        with patch("local.batch.source_state", return_value=self.state), \
             patch("local.batch.run_process", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "DLL 验证子进程 退出码"):
                self.batch.ensure_native_compatibility([self.task])
        self.assertEqual(self.config["native_profile"].read_bytes(), self.original)
        self.assertEqual(self.journal.entries[-1]["status"], "failed")

    def test_invalid_media_keeps_original_configuration(self):
        """完整解码失败时拒绝更新指纹。"""
        with patch("local.batch.source_state", return_value=self.state), \
             patch("local.batch.run_process", side_effect=self.fake_process), \
             patch.object(self.batch, "mux_candidate", side_effect=self.fake_mux), \
             patch.object(self.batch, "media", return_value={}), \
             patch.object(self.batch, "verify", side_effect=ValueError("解码失败")):
            with self.assertRaisesRegex(ValueError, "解码失败"):
                self.batch.ensure_native_compatibility([self.task])
        self.assertEqual(self.config["native_profile"].read_bytes(), self.original)

    def test_external_edit_is_not_overwritten(self):
        """验收期间用户修改配置时保留外部编辑，并拒绝写入新指纹。"""
        def edit(*args, **kwargs):
            """模拟验证期间追加用户注释；接收验证参数，无返回值。"""
            with self.config["native_profile"].open("ab") as stream:
                stream.write(b"# external edit\n")

        with patch("local.batch.source_state", return_value=self.state), \
             patch("local.batch.run_process", side_effect=self.fake_process), \
             patch.object(self.batch, "mux_candidate", side_effect=self.fake_mux), \
             patch.object(self.batch, "media", return_value={}), \
             patch.object(self.batch, "verify", side_effect=edit):
            with self.assertRaisesRegex(RuntimeError, "配置在验收期间被修改"):
                self.batch.ensure_native_compatibility([self.task])
        self.assertEqual(self.config["native_profile"].read_bytes(), self.original + b"# external edit\n")

    def test_changed_dll_during_validation_is_rejected(self):
        """验收期间 DLL 内容再次变化时，不更新原配置。"""
        def change(*args, **kwargs):
            """模拟客户端覆盖候选 DLL；接收验证参数，无返回值。"""
            self.dll.write_bytes(b"changed again")

        with patch("local.batch.source_state", return_value=self.state), \
             patch("local.batch.run_process", side_effect=self.fake_process), \
             patch.object(self.batch, "mux_candidate", side_effect=self.fake_mux), \
             patch.object(self.batch, "media", return_value={}), \
             patch.object(self.batch, "verify", side_effect=change):
            with self.assertRaisesRegex(RuntimeError, "客户端版本或文件发生变化"):
                self.batch.ensure_native_compatibility([self.task])
        self.assertEqual(self.config["native_profile"].read_bytes(), self.original)

    def test_no_sample_cannot_update_digest(self):
        """没有已完成视频时不尝试加载候选 DLL，也不更新配置。"""
        with self.assertRaisesRegex(RuntimeError, "没有已完成视频"):
            self.batch.ensure_native_compatibility([])
        self.assertEqual(self.config["native_profile"].read_bytes(), self.original)

    def test_changed_source_during_validation_is_rejected(self):
        """完整解码后来源变化时保留原指纹。"""
        changed = {**self.state, "playlist_sha256": "changed"}
        with patch("local.batch.source_state", side_effect=[self.state, changed]), \
             patch("local.batch.run_process", side_effect=self.fake_process), \
             patch.object(self.batch, "mux_candidate", side_effect=self.fake_mux), \
             patch.object(self.batch, "media", return_value={}), \
             patch.object(self.batch, "verify", return_value={}):
            with self.assertRaisesRegex(ValueError, "来源文件或清单发生变化"):
                self.batch.ensure_native_compatibility([self.task])
        self.assertEqual(self.config["native_profile"].read_bytes(), self.original)

    def test_already_updated_by_other_batch_does_not_revalidate(self):
        """另一批次已更新为当前 DLL 指纹时，重新读取配置并跳过重复验收。"""
        self.config["native_profile"].write_bytes(self.original.replace(self.old_digest.encode(), self.new_digest.encode()))
        with patch("local.batch.run_process") as process:
            self.batch.ensure_native_compatibility([self.task])
        process.assert_not_called()
        self.assertEqual(self.config["native"]["dll_sha256"], self.new_digest)

    def test_digest_update_preserves_bom_crlf_quotes_and_comment(self):
        """替换带 BOM、CRLF、引号和行尾注释的 YAML 指纹时保留其他字节。"""
        path = self.config["native_profile"]
        original = (b"\xef\xbb\xbf# profile\r\ndll_sha256: '" + self.old_digest.encode()
                    + b"' # keep\r\n")
        path.write_bytes(original)
        update_digest(path, original, self.old_digest, self.new_digest)
        self.assertEqual(path.read_bytes(), original.replace(self.old_digest.encode(), self.new_digest.encode()))

    def test_profile_lock_waits_and_honors_cancellation(self):
        """并发验收等待同一配置锁，等待期间可取消且不会获得锁。"""
        cancel, started, finished = Event(), Event(), Event()
        outcome = []

        def wait_for_lock():
            """等待当前配置锁并记录取消异常；无参数或返回值。"""
            started.set()
            try:
                with profile_lock(self.config["native_profile"], cancel, 0.01):
                    outcome.append("acquired")
            except TaskCancelled:
                outcome.append("cancelled")
            finally:
                finished.set()

        with profile_lock(self.config["native_profile"], Event(), 0.01):
            worker = Thread(target=wait_for_lock)
            worker.start()
            self.assertTrue(started.wait(5))
            self.assertFalse(finished.wait(0.1))
            cancel.set()
            self.assertTrue(finished.wait(5))
        worker.join(5)
        self.assertEqual(outcome, ["cancelled"])

    def test_changed_dll_during_batch_is_validated_before_extraction(self):
        """还原槽已持有时可验收；加载前再次变化时重试，并使用更新指纹处理剧集。"""
        self.config["output_path"] = self.root / "output"
        task = {**self.task, "series": "series", "series_id": "samplecid", "fid": "sample.hls"}
        outcome = []
        attempts = []

        def fake_extract(config, segments, folder, target, **kwargs):
            """核对 config 的指纹已更新，写入 target 并返回来源摘要。"""
            self.assertEqual(config["native"]["dll_sha256"], self.new_digest)
            attempts.append(1)
            if len(attempts) == 1:
                raise NativeFingerprintMismatch("模拟加载前 DLL 再次变化")
            target.write_bytes(b"media")
            return self.state["sources"]

        def run_job():
            """执行隔离任务并记录返回值或异常，无参数或返回值。"""
            try:
                outcome.append(self.batch.job(task, self.journal.add(task)))
            except Exception as error:
                outcome.append(error)

        proof = {"device": "cpu", "frames": 1, "duration_seconds": 1, "exit_code": 0}
        with patch("local.batch.source_state", return_value=self.state), \
             patch("local.batch.run_process", side_effect=self.fake_process), \
             patch("local.batch.extract", side_effect=fake_extract), \
             patch.object(self.batch, "mux_candidate", side_effect=self.fake_mux), \
             patch.object(self.batch, "media", return_value={}), \
             patch.object(self.batch, "verify", return_value=proof):
            worker = Thread(target=run_job, daemon=True)
            worker.start()
            worker.join(5)
        self.assertFalse(worker.is_alive(), "持有还原槽时兼容验收发生死锁")
        self.assertEqual(outcome[0]["status"], "completed")
        self.assertEqual(self.config["native"]["dll_sha256"], self.new_digest)
        self.assertEqual(len(attempts), 2)


if __name__ == "__main__":
    unittest.main()
