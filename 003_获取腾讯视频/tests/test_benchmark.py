"""验证性能基准隔离工作目录、重复运行和报告保存。"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local.configuration import load_config
from tests.benchmark_batch import benchmark


class BenchmarkTests(unittest.TestCase):
    """使用模拟批次验证基准目录隔离，不处理真实媒体。"""

    def test_repeat_uses_fresh_directories_and_cleans_them(self):
        """每轮使用不同空目录并清理，报告保存各轮结果和资源记录。"""
        with tempfile.TemporaryDirectory(prefix="tencent_benchmark_test_") as root:
            root = Path(root)
            config = deepcopy(load_config(Path("configs/config.yaml"), source="download"))
            config["output_path"], config["work_dir"] = root / "output", root / "work"
            paths = []

            def fake_batch(settings):
                """检查 settings 中目录为空，创建临时成品并返回成功汇总。"""
                output, work = settings["output_path"], settings["work_dir"]
                self.assertEqual(list(output.iterdir()), [])
                self.assertEqual(list(work.iterdir()), [])
                self.assertEqual(settings["selection"], {"video_ids": ["a"], "series_names": []})
                paths.append((output, work))
                (output / "candidate.mp4").write_bytes(b"test")
                (output / settings["logging"]["state_file"]).write_text(
                    json.dumps({"runs": [{"entries": []}]}), encoding="utf-8")
                return {"completed": 1, "failed": 0, "interrupted": 0}

            def fake_sample(stop, interval, samples):
                """向 samples 写入固定测试记录；其余参数与采样接口一致。"""
                samples.append({"cpu_percent": 1})

            report = root / "report.json"
            with patch("tests.benchmark_batch.discover_all", return_value=([{"video_id": "a", "media_bytes": 1}], [])), \
                 patch("tests.benchmark_batch.latest_dll", return_value=root / "client.dll"), \
                 patch("tests.benchmark_batch.run_batch", side_effect=fake_batch), \
                 patch("tests.benchmark_batch.sample_resources", side_effect=fake_sample):
                benchmark(config, 2, 1, report)
            saved = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["runs"]), 2)
            self.assertNotEqual(paths[0], paths[1])
            self.assertTrue(all(not path.exists() for pair in paths for path in pair))
            self.assertEqual(saved["runs"][0]["system_samples"], [{"cpu_percent": 1}])


if __name__ == "__main__":
    unittest.main()
