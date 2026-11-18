"""验证正式入口的配置路径选择；不启动下载或访问客户端。"""

from pathlib import Path
import unittest
from unittest.mock import patch

import decode


class ConfigArgumentTests(unittest.TestCase):
    """检查默认配置基于入口目录，且显式配置可以覆盖默认值。"""

    def test_default_and_explicit_config(self):
        """模拟无参数及显式路径两种启动方式，核对传入配置加载器的路径。"""
        default = Path(decode.__file__).resolve().parent / "configs/config.yaml"
        custom = Path("custom.yaml")
        summary = {"failed": 0, "skipped": 0, "interrupted": 0}
        for args, expected in ((["decode.py"], default), (["decode.py", "--config", str(custom)], custom)):
            with self.subTest(args=args), patch("sys.argv", args), \
                    patch("decode.load_config", return_value={}) as load, \
                    patch("decode.run_url_batch", return_value=summary):
                self.assertEqual(decode.main(), 0)
                load.assert_called_once_with(expected)
