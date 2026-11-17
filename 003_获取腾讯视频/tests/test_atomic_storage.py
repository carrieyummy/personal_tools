"""隔离验证 Windows 共享占用下 JSON 原子替换的恢复、有界失败和原文件保护。"""

from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Timer
import unittest
from unittest.mock import patch

from local.configuration import load_config
from local.journal import Journal
from local.storage import atomic_json, validate_atomic_retry


class AtomicStorageTests(unittest.TestCase):
    """以当前配置为基础构造共享冲突，不接触真实视频或生产日志。"""

    def setUp(self):
        """读取当前策略并创建独立目录及旧版本记录；无返回值。"""
        self.config = load_config(Path(__file__).resolve().parents[1] / "configs/config.yaml", source="url")
        self.policy = deepcopy(self.config["logging"]["atomic_replace"])
        temporary = TemporaryDirectory(prefix="tencent_atomic_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "record.json"
        atomic_json(self.path, {"revision": "old"})
        self.old = self.path.read_bytes()

    def busy_error(self, code: int) -> PermissionError:
        """按 code 构造 Windows 权限/共享错误，返回异常，不依赖当前操作系统。"""
        error = PermissionError(13, "synthetic sharing conflict")
        error.winerror = code
        return error

    def test_transient_reader_conflict_preserves_atomic_write(self):
        """短暂占用解除后保存完整新记录；重试期间原记录完整可读，不出现截断。"""
        self.policy["attempts"] = 3
        original = Path.replace
        seen = []
        def replace_file(source, target):
            """模拟 source 到 target 的前两次共享冲突，记录原目标内容，之后执行真实替换。"""
            seen.append(json.loads(target.read_text(encoding="utf-8")))
            if len(seen) < self.policy["attempts"]:
                raise self.busy_error(5)
            return original(source, target)
        with patch.object(Path, "replace", replace_file), patch("local.storage.time.sleep") as delay:
            atomic_json(self.path, {"revision": "new"}, retry=self.policy)
        self.assertEqual(seen, [{"revision": "old"}] * self.policy["attempts"])
        self.assertEqual(delay.call_count, self.policy["attempts"] - 1)
        delay.assert_called_with(self.policy["interval_seconds"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"revision": "new"})
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_retry_exhaustion_keeps_old_record_and_cleans_temporary(self):
        """永久占用在配置次数耗尽后抛错，保留原文件并清理本次临时文件。"""
        self.policy["attempts"] = 3
        for code in (5, 32, 33):
            with self.subTest(code=code), patch.object(Path, "replace", side_effect=self.busy_error(code)) as operation, patch("local.storage.time.sleep") as delay:
                with self.assertRaises(PermissionError):
                    atomic_json(self.path, {"revision": "new"}, retry=self.policy)
                self.assertEqual(operation.call_count, self.policy["attempts"])
                self.assertEqual(delay.call_count, self.policy["attempts"] - 1)
            self.assertEqual(self.path.read_bytes(), self.old)
            self.assertFalse(list(self.root.glob("*.tmp")))

    def test_unrelated_errors_and_no_retry_policy_fail_immediately(self):
        """其他 I/O 故障以及未指定策略的调用立即失败，不套用日志重试。"""
        for error, policy in ((OSError(28, "synthetic disk full"), self.policy),
                              (self.busy_error(1314), self.policy), (self.busy_error(5), None)):
            with self.subTest(error=error), patch.object(Path, "replace", side_effect=error) as operation, patch("local.storage.time.sleep") as delay:
                with self.assertRaises(OSError):
                    atomic_json(self.path, {"revision": "new"}, retry=policy)
                operation.assert_called_once()
                delay.assert_not_called()
            self.assertEqual(self.path.read_bytes(), self.old)

    def test_invalid_policy_is_rejected_before_writing(self):
        """次数和间隔必须是配置允许的有限正值，非法值不开始写盘。"""
        for key, values in (("attempts", (0, -1, True, 1.5)), ("interval_seconds", (0, -1, True, float("inf"), float("nan")))):
            for value in values:
                policy = {**self.policy, key: value}
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    validate_atomic_retry(policy)
                with self.assertRaises(ValueError):
                    atomic_json(self.path, {"revision": "new"}, retry=policy)
                self.assertEqual(self.path.read_bytes(), self.old)

    @unittest.skipUnless(os.name == "nt", "需要真实 Windows 文件共享语义")
    def test_journal_update_survives_real_reader_handle(self):
        """真实读句柄短暂阻止替换时，日志更新等待占用解除后成功，不把剧集标记为失败。"""
        config = deepcopy(self.config)
        config["output_path"] = self.root
        config["logging"].update(port=0, final_state_timeout_seconds=0.1)
        # 测试独立指定足够的重试预算；不要求用户当前策略提供重试。
        config["logging"]["atomic_replace"]["attempts"] = 20
        journal = Journal(config)
        try:
            entry = journal.add({"video_id": "synthetic-video", "title": "共享占用测试"})
            reader = journal.path.open("rb")
            timer = Timer(config["logging"]["atomic_replace"]["interval_seconds"] * 2, reader.close)
            timer.start()
            try:
                journal.update(entry, "downloading", status="running", detail="读取媒体")
            finally:
                reader.close()
                timer.join()
            self.assertEqual(entry["status"], "running")
            self.assertEqual(json.loads(journal.path.read_text(encoding="utf-8"))["runs"][-1]["entries"][0]["stage"], "downloading")
        finally:
            journal.close("completed", {})


if __name__ == "__main__":
    unittest.main()
