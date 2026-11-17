"""以当前配置的同步屏障验证解析不占下载名额，下载满载时仍可解析下一批任务。"""
from threading import Event, Lock, current_thread
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from local.configuration import read_config
from local.scheduler import run_pipeline


class ConcurrencyLimitsTests(unittest.TestCase):
    """根据配置取得下载和处理上限，使用事件而非固定延时观察占用。"""

    def test_downloads_overlap_with_separate_bounded_resolution(self):
        """下载池达到配置上限时，解析在共享处理池继续；释放后所有任务完成。"""
        config = read_config(Path(__file__).resolve().parents[1] / "configs/config.yaml")
        downloads, processing = config["download"]["threads"], config["max_in_flight"]
        overlap = min(downloads, processing)
        full, parsing, release, lock = Event(), Event(), Event(), Lock()
        active, peaks, done = [0, 0], [0, 0], []

        def steps(index):
            """解析 index 后转入下载，最后交回处理池；返回模拟成功报告。"""
            self.assertTrue(current_thread().name.startswith("mgtv-process"))
            with lock:
                active[1] += 1
                peaks[1] = max(peaks[1], active[1])
                if index >= downloads and active[1] == overlap:
                    parsing.set()
            try:
                if index >= downloads:
                    self.assertTrue(full.wait(10))
                    self.assertTrue(release.wait(10))
            finally:
                with lock:
                    active[1] -= 1
            yield "download", 0
            self.assertTrue(current_thread().name.startswith("mgtv-download"))
            with lock:
                active[0] += 1
                peaks[0] = max(peaks[0], active[0])
                if active[0] == downloads:
                    full.set()
            try:
                self.assertTrue(release.wait(10))
            finally:
                with lock:
                    active[0] -= 1
            yield "ready", 1
            self.assertTrue(current_thread().name.startswith("mgtv-process"))
            done.append(index)
            return "成功"

        def run():
            """用生产调度运行合成剧集，无返回值。"""
            run_pipeline(range(downloads + overlap), config, steps, lambda *args: None, Event(), Event(),
                         downloaded_size=lambda value: value, waiting=lambda *args: None)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run)
            try:
                self.assertTrue(full.wait(5))
                self.assertTrue(parsing.wait(5))
                with lock:
                    self.assertEqual(active, [downloads, overlap])
            finally:
                release.set()
            future.result(timeout=15)
        self.assertEqual(len(done), downloads + overlap)
        self.assertEqual(peaks[0], downloads)
        self.assertLessEqual(peaks[1], processing)
