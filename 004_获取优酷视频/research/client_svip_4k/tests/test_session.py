"""验证会话只驻内存、网络目的地限制及报告脱敏，不向真实网络发送测试请求。"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from lib.native import signature_summary
from lib.online import cookie_value, load_session, require_youku_https, response_status
from lib.probe import load_config, stream_summary

CONFIG = Path(__file__).resolve().parents[1] / "configs/research.yaml"


class SessionTests(unittest.TestCase):
    """验证登录态复用边界；无构造参数，通过 unittest 返回检查结果。"""

    def test_destination_rejects_lookalike_hosts(self) -> None:
        """仅允许优酷 HTTPS，拒绝伪装域名；无参数和返回值。"""
        config = load_config(CONFIG)
        require_youku_https(config["network"]["base_url"])
        for url in ("http://acs.youku.com/h5", "https://youku.com.evil.test/", "https://evil-youku.com/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                require_youku_https(url)

    def test_session_database_is_not_modified_or_exported(self) -> None:
        """从最小数据库读取会话但不改源文件、不建导出文件；无参数和返回值。"""
        config = load_config(CONFIG)
        name = config["network"]["token_cookie"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "Cookies"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE cookies(host_key,name,value,path,is_secure,expires_utc,encrypted_value)")
                db.execute("INSERT INTO cookies VALUES (?,?,?,?,?,?,?)",
                           (".youku.com", name, "fake-test-session", "/", 1, 0, b""))
            before = path.read_bytes()
            jar = load_session(dict(config, user_data=root))
            self.assertEqual(cookie_value(jar, name), "fake-test-session")
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(list(root.iterdir()), [path])

    def test_native_summary_does_not_export_signature(self) -> None:
        """原生诊断仅输出存在性和长度；无参数和返回值。"""
        summary = signature_summary({"ckey": "fake-sensitive-signature", "encryptR1": "fake-challenge"})
        self.assertTrue(summary["signature_present"])
        self.assertNotIn("fake-", json.dumps(summary))

    def test_response_does_not_export_opaque_error_detail(self) -> None:
        """接口状态只保存错误代码；无参数和返回值。"""
        summary = response_status({"ret": ["FAIL_TEST::fake-sensitive-error-detail"]})
        self.assertEqual(summary["ret_codes"], ["FAIL_TEST"])
        self.assertNotIn("fake-sensitive", json.dumps(summary))

    def test_stream_features_do_not_export_nested_authorization(self) -> None:
        """特征列表不得递归导出内部对象；无参数和返回值。"""
        config = load_config(CONFIG)
        field = config["cache"]["stream_fields"][0]
        stream = {field: field, "stream_ext": {"feature_list": [{"copyright_key": "fake-secret"}]}}
        result = stream_summary({"stream": [stream]}, config["cache"]["stream_fields"])
        self.assertNotIn("fake-secret", json.dumps(result))
