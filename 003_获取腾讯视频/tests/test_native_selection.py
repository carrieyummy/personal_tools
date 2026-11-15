"""验证客户端版本目录选择及缺少最新 DLL 时的行为。"""

from pathlib import Path
import hashlib
import tempfile
import unittest

from local.configuration import load_config
from local.native import latest_dll, verified_dll


class NativeSelectionTests(unittest.TestCase):
    """使用隔离版本目录验证 DLL 选择，不加载真实客户端库。"""

    def setUp(self):
        """创建临时客户端目录，并从实际配置读取 DLL 文件名；无参数或返回值。"""
        self.temporary = tempfile.TemporaryDirectory(prefix="tencent_versions_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.name = load_config(Path("configs/config.yaml"), source="download")["native"]["dll_name"]

    def test_latest_version_is_compared_numerically(self):
        """按数字段选最新版本，忽略非版本目录；无参数或返回值。"""
        for version in ("2.9", "2.10", "10.1"):
            folder = self.root / version
            folder.mkdir()
            (folder / self.name).touch()
        (self.root / "other").mkdir()
        self.assertEqual(latest_dll(self.root, self.name), self.root / "10.1" / self.name)

    def test_missing_latest_dll_does_not_fall_back(self):
        """最新目录缺少 DLL 时明确失败，不回退旧版；无参数或返回值。"""
        old = self.root / "1.0"
        old.mkdir()
        (old / self.name).touch()
        (self.root / "2.0").mkdir()
        with self.assertRaises(FileNotFoundError):
            latest_dll(self.root, self.name)

    def test_missing_version_directory_is_reported(self):
        """没有数字版本目录时明确失败；无参数或返回值。"""
        with self.assertRaises(FileNotFoundError):
            latest_dll(self.root, self.name)

    def test_latest_dll_requires_verified_fingerprint(self):
        """最新 DLL 与配置摘要不一致时拒绝加载，不回退到摘要匹配的旧版本。"""
        old, new = self.root / "1.0", self.root / "2.0"
        old.mkdir()
        new.mkdir()
        (old / self.name).write_bytes(b"verified old")
        (new / self.name).write_bytes(b"unverified new")
        profile = {"dll_root": self.root, "dll_name": self.name,
                   "dll_sha256": hashlib.sha256(b"verified old").hexdigest()}
        with self.assertRaisesRegex(RuntimeError, "指纹与接口配置不匹配"):
            verified_dll(profile)
        profile["dll_sha256"] = hashlib.sha256(b"unverified new").hexdigest()
        self.assertEqual(verified_dll(profile), new / self.name)


if __name__ == "__main__":
    unittest.main()
