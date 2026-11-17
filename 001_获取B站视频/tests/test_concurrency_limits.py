"""验证下载数与共享处理上限独立，准备使用处理池且复用跳过下载池。"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, current_thread
import unittest

from bilibili_downloader.config import load_settings
from bilibili_downloader.scheduler import Ready, run_pipeline
from tests import CONFIG_PATH


class ConcurrencyLimitsTests(unittest.TestCase):
    """按实际配置构造同步屏障，不固定生产并发默认值。"""

    def test_full_download_pool_overlaps_with_resolution(self):
        """下载名额满载时，下一批准备仍能在共享处理池执行；释放后全部完成。"""
        settings = load_settings(CONFIG_PATH)
        downloads, processing = settings.threads, settings.max_in_flight
        overlap = min(downloads, processing)
        full, parsing, release, lock = Event(), Event(), Event(), Lock()
        active, peaks, completed = [0, 0], [0, 0], []

        def prepare(job):
            """在共享池准备 job，第二批等待测试释放；返回任务编号。"""
            self.assertTrue(current_thread().name.startswith('bilibili-process'))
            with lock:
                active[1] += 1
                peaks[1] = max(peaks[1], active[1])
                if job >= downloads and active[1] == overlap:
                    parsing.set()
            try:
                if job >= downloads:
                    self.assertTrue(full.wait(10))
                    self.assertTrue(release.wait(10))
                return job
            finally:
                with lock:
                    active[1] -= 1

        def download(job, cancel):
            """下载 job 至事件释放；cancel 为取消信号，返回编号和模拟字节数。"""
            self.assertTrue(current_thread().name.startswith('bilibili-download'))
            with lock:
                active[0] += 1
                peaks[0] = max(peaks[0], active[0])
                if active[0] == downloads:
                    full.set()
            try:
                self.assertTrue(release.wait(10))
                return job, 1
            finally:
                with lock:
                    active[0] -= 1

        def process(value, cancel):
            """确认 value 的后处理在线程池执行；cancel 为取消信号，返回原编号。"""
            self.assertTrue(current_thread().name.startswith('bilibili-process'))
            return value[0]

        def complete(job, result, error):
            """断言 job 的 result/error 并记录完成；无返回值。"""
            self.assertIsNone(error)
            self.assertEqual(job, result)
            completed.append(job)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_pipeline, list(range(downloads + overlap)), settings, prepare, download,
                                 process, complete, Event(), downloaded_size=lambda value: value[1],
                                 waiting=lambda *args: None, finishers=deque())
            try:
                self.assertTrue(full.wait(5))
                self.assertTrue(parsing.wait(5))
                with lock:
                    self.assertEqual(active, [downloads, overlap])
            finally:
                release.set()
            future.result(timeout=15)
        self.assertEqual(len(completed), downloads + overlap)
        self.assertLessEqual(peaks[1], processing)

    def test_local_reuse_skips_download_pool(self):
        """准备返回完整本地媒体时，直接进入共享处理池，不调用下载函数。"""
        settings, completed = load_settings(CONFIG_PATH), []
        run_pipeline([0], settings, lambda job: Ready((job, 0)),
                     lambda *args: self.fail('复用不应下载'), lambda value, cancel: value[0],
                     lambda job, result, error: completed.append((result, error)), Event(),
                     downloaded_size=lambda value: value[1], waiting=lambda *args: None, finishers=deque())
        self.assertEqual(completed, [(0, None)])
