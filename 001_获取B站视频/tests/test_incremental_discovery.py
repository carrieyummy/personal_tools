"""以阻塞目录和合成媒体验证增量发现，不读取真实账号、不访问外部网络。"""
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import Event, Lock, Thread, current_thread, enumerate as enumerate_threads
import unittest
from unittest.mock import patch

from dataclasses import replace
from bilibili_downloader.config import load_settings
from bilibili_downloader.errors import check_cancel
from bilibili_downloader.scheduler import run_pipeline

class IncrementalDiscoveryTests(unittest.TestCase):
    """真实共享调度器的增量目录交接检查；接口和媒体均由事件替身控制。"""

    def configure(self):
        """从当前配置派生单下载、单处理名额，路径及其他配置不写死为生产默认值。"""
        self.settings = replace(load_settings(Path(__file__).resolve().parents[1]/'config/config.yaml'), threads=1, max_in_flight=1)
        self.interval = self.settings.cancel_poll_interval
        self.process_prefix, self.download_prefix = 'bilibili-process', 'bilibili-download'

    def run_pipeline(self, source):
        """以生产 B 站调度器的独立回调接口推进 source 及对应阶段生成器。"""
        def prepare(index):
            """在处理池推进 index 的初始解析，返回下载载荷。"""
            generator = self.steps(index)
            self.assertEqual(next(generator)[0], 'download')
            return index, generator
        def download(payload, cancel):
            """在下载池推进 payload；cancel 为生产取消事件，返回字节证据。"""
            index, generator = payload
            self.assertEqual(next(generator), ('ready',1))
            return index, generator, 1
        def process(payload, cancel):
            """在处理池推进 payload 的后处理；cancel 为生产取消事件，返回完成结果。"""
            try:
                next(payload[1])
            except StopIteration as done:
                return done.value
            raise AssertionError('unexpected stage')
        def complete(index, result, error):
            """记录 index 的 result/error，不运行任何真实媒体工具。"""
            self.record(index, 'interrupted' if error else result['status'], error)
        run_pipeline([], self.settings, prepare, download, process, complete, self.cancel,
                     downloaded_size=lambda value:value[2], waiting=self.record, finishers=deque(),
                     discoveries=source, discovered=lambda event:[] if event is None else [event])

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

