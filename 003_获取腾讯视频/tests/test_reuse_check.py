"""验证已发布成品的快速检查、完整校验和旧索引升级。"""

from copy import deepcopy
import os
from pathlib import Path
import tempfile
from threading import Event, Lock
import unittest
from unittest.mock import patch
import yaml

from local.batch import check_record, load_index
from local.configuration import load_config
from local.storage import atomic_json, fingerprint


class ReuseCheckTests(unittest.TestCase):
    """使用隔离成品和真实索引验证复用检查，不访问真实成品。"""

    def setUp(self):
        """从实际配置复制参数并创建临时成品及旧格式索引，无返回值。"""
        self.temp = tempfile.TemporaryDirectory(prefix="tencent_reuse_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = deepcopy(load_config(Path("configs/config.yaml")))
        self.config["reuse_check"] = "fast"
        self.path = self.root / "episode.mp4"
        self.path.write_bytes(b"original")
        fp = fingerprint(self.path, self.config["copy_buffer_bytes"], force=True)
        self.item = {"file": self.path.name, "bytes": fp["size_bytes"], "sha256": fp["sha256"]}
        self.index = self.root / "video_info.json"
        atomic_json(self.index, {"version": 1, "files": [self.item]})
        self.cancel, self.lock = Event(), Lock()

    def check(self, item):
        """按当前配置核对记录 item，返回复用检查结果。"""
        return check_record(self.config, self.index, self.path, item, self.cancel, self.lock)

    def test_legacy_record_is_verified_once_then_fast(self):
        """旧记录首次计算摘要并保存时间戳，之后快速检查不读取成品内容。"""
        with patch("local.storage.hashlib.sha256", wraps=__import__("hashlib").sha256) as digest:
            self.assertTrue(self.check(self.item))
            self.assertTrue(digest.called)
        item = load_index(self.index)["files"][0]
        self.assertEqual(item["mtime_ns"], self.path.stat().st_mtime_ns)
        with patch("local.storage.hashlib.sha256", side_effect=AssertionError("快速检查不应计算摘要")):
            self.assertTrue(self.check(item))

    def test_changed_file_rejected_and_identical_touch_rechecked(self):
        """修改时间变化时重新计算摘要；内容不变可更新记录，内容改变则拒绝复用。"""
        self.assertTrue(self.check(self.item))
        item = load_index(self.index)["files"][0]
        stamp = item["mtime_ns"] + 2_000_000_000
        os.utime(self.path, ns=(stamp, stamp))
        self.assertTrue(self.check(item))
        item = load_index(self.index)["files"][0]
        self.assertEqual(item["mtime_ns"], self.path.stat().st_mtime_ns)
        self.path.write_bytes(b"changed!")
        os.utime(self.path, ns=(stamp + 2_000_000_000, stamp + 2_000_000_000))
        self.assertFalse(self.check(item))
        self.assertEqual(load_index(self.index)["files"][0], item)

    def test_full_detects_content_change_with_same_metadata(self):
        """完整模式重算摘要，能发现大小和修改时间未变的内容变化。"""
        self.assertTrue(self.check(self.item))
        item = load_index(self.index)["files"][0]
        self.path.write_bytes(b"changed!")
        os.utime(self.path, ns=(item["mtime_ns"], item["mtime_ns"]))
        self.assertTrue(self.check(item))
        self.config["reuse_check"] = "full"
        self.assertFalse(self.check(item))

    def test_invalid_digest_is_not_trusted(self):
        """摘要无效时即使元数据匹配也不能复用成品。"""
        self.item.update(mtime_ns=self.path.stat().st_mtime_ns, sha256="invalid")
        self.assertFalse(self.check(self.item))

    def test_configuration_rejects_invalid_modes(self):
        """加载主配置时拒绝无效检查方式；其他配置沿用用户当前 YAML。"""
        original_load = yaml.safe_load
        for mode in ("unknown", None, True):
            with self.subTest(mode=mode):
                calls = 0

                def load_with_mode(data):
                    """解析 YAML 数据 data，首份主配置的检查模式替换为当前测试值并返回。"""
                    nonlocal calls
                    result = original_load(data)
                    calls += 1
                    if calls == 1:
                        result["reuse_check"] = mode
                    return result

                with patch("local.configuration.yaml.safe_load", side_effect=load_with_mode):
                    with self.assertRaisesRegex(ValueError, "reuse_check"):
                        load_config(Path("configs/config.yaml"))


if __name__ == "__main__":
    unittest.main()
