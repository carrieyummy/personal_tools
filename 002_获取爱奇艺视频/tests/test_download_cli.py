"""验证链接下载入口的配置路径选择，不执行真实下载。"""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import download_iqiyi


class DownloadConfigTests(unittest.TestCase):
    """验证默认配置独立于当前目录，且显式配置可覆盖默认值。"""

    def test_default_and_explicit_config(self):
        """分别检查省略配置和指定配置时传给下载业务的路径；无参数或返回值。"""
        default_config = Path(download_iqiyi.__file__).resolve().parent / "configs" / "playback.yaml"
        report = {"failed": 0, "link_errors": []}
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                for arguments, expected in (([], default_config), (["--config", "custom.yaml"], Path("custom.yaml"))):
                    with (patch.object(sys, "argv", ["download_iqiyi.py", *arguments, "--plan"]),
                          patch("download_iqiyi.run_downloads", return_value=(report, Path("report.json"))) as run,
                          redirect_stdout(StringIO())):
                        self.assertEqual(download_iqiyi.main(), 0)
                        run.assert_called_once_with(expected, None, True, None)
            finally:
                os.chdir(original_cwd)
