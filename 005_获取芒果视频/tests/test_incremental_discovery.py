"""以阻塞目录和合成媒体验证增量发现，不读取真实账号、不访问外部网络。"""
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import Event, Lock, Thread, current_thread, enumerate as enumerate_threads
import unittest
from unittest.mock import patch

from local.client import check_cancel
from local.configuration import read_config
from local.scheduler import run_pipeline

class IncrementalDiscoveryTests(unittest.TestCase):
    """真实共享调度器的增量目录交接检查；接口和媒体均由事件替身控制。"""

    def configure(self):
        """读取当前配置，再派生下载和处理各一个名额的受控边界。"""
        self.config = read_config(Path(__file__).resolve().parents[1]/'configs/config.yaml')
        self.config['download']['threads'] = self.config['max_in_flight'] = 1
        self.interval = self.config['runtime']['cancel_poll_interval_seconds']
        self.process_prefix, self.download_prefix = 'mgtv-process', 'mgtv-download'

    def run_pipeline(self, source):
        """用生产阻塞来源模式调度 source，收集任务终态和下载完成通知。"""
        def complete(index, value, error):
            """把 index 的 value/error 映射为测试终态。"""
            self.record(index, 'interrupted' if error else value['status'], error)
        run_pipeline(source, self.config, self.steps, complete, self.cancel, Event(),
                     downloaded_size=lambda value:value, waiting=self.record, discover_in_pool=True)

    def setUp(self):
        """加载当前配置并派生单名额边界；初始化取消、目录、下载及完成通知事件。"""
        self.configure()
        self.cancel, self.started, self.entered = Event(), Event(), Event()
        self.release_catalog, self.release_media, self.received = Event(), Event(), Event()
        self.guard, self.active, self.peak = Lock(), 0, 0
        self.outcomes, self.errors = {}, []

    @contextmanager
    def occupied(self):
        """统计目录、逐项准备和后处理的共用并发；检查线程归属并在退出释放计数。"""
        self.assertTrue(current_thread().name.startswith(self.process_prefix))
        with self.guard:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            yield
        finally:
            with self.guard:
                self.active -= 1

    def source(self):
        """先交接首项再阻塞后续目录，直到释放或取消；每次查询只占共享处理池一个名额。"""
        with self.occupied():
            pass
        yield 0
        with self.occupied():
            if not self.started.wait(10):
                raise TimeoutError('first download did not start')
            self.entered.set()
            while not self.release_catalog.wait(self.interval):
                check_cancel(self.cancel)
        yield 1

    def steps(self, index):
        """按 index 模拟解析、下载与后处理，分别检查线程归属；返回完成状态。"""
        with self.occupied():
            pass
        yield 'download', 0
        self.assertTrue(current_thread().name.startswith(self.download_prefix))
        self.started.set()
        while not self.release_media.wait(self.interval):
            check_cancel(self.cancel)
        check_cancel(self.cancel)
        yield 'ready', 1
        with self.occupied():
            return {'status':'completed'}

    def record(self, index, stage, value=None):
        """记录 index 的 stage/value；队列通知证明主循环没有被后续目录查询卡住。"""
        self.outcomes[index] = stage
        if stage in ('waiting_queue', '等待队列容量'):
            self.received.set()

    def start(self):
        """后台执行生产调度器并保留批次异常；返回可有界等待的测试线程。"""
        def run():
            """运行当前项目适配的调度调用；异常只收集供主线程断言。"""
            try:
                self.run_pipeline(self.source())
            except BaseException as error:
                self.errors.append(error)
        thread = Thread(target=run)
        thread.start()
        return thread

    def test_slow_catalog_does_not_block_download_or_completion_notifications(self):
        """单处理名额下慢目录与下载重叠，下载完成仍被主循环接收；总处理并发不叠加。"""
        thread = self.start()
        try:
            self.assertTrue(self.started.wait(5))
            self.assertTrue(self.entered.wait(5))
            self.release_media.set()
            self.assertTrue(self.received.wait(5))
            self.assertFalse(self.release_catalog.is_set())
            self.assertLessEqual(self.peak, 1)
        finally:
            self.release_catalog.set()
            self.release_media.set()
            thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.errors, [])
        self.assertEqual(self.outcomes, {0:'completed', 1:'completed'})
        self.assertEqual(self.active, 0)

    def test_cancel_during_slow_catalog_reclaims_shared_workers(self):
        """目录与媒体同时等待时取消，不发现下一任务，退出时无残留工作者及处理占用。"""
        thread = self.start()
        try:
            self.assertTrue(self.started.wait(5))
            self.assertTrue(self.entered.wait(5))
        finally:
            self.cancel.set()
            self.release_media.set()
            thread.join(15)
            self.release_catalog.set()
        self.assertFalse(thread.is_alive())
        self.assertNotIn(1, self.outcomes)
        self.assertEqual(self.active, 0)
        self.assertFalse(any(t.name.startswith((self.process_prefix, self.download_prefix)) for t in enumerate_threads()))

