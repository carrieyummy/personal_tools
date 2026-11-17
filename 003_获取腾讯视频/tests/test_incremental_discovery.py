"""以阻塞目录和合成媒体验证增量发现，不读取真实账号、不访问外部网络。"""
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import Event, Lock, Thread, current_thread, enumerate as enumerate_threads
import unittest
from unittest.mock import patch

from local.playback.application import UrlBatch
from local.playback.tasks import Discovery, Request
from local.runtime import check_cancel
from tests.test_batch import MemoryJournal
from tests.test_playback import current_config

class IncrementalDiscoveryTests(unittest.TestCase):
    """真实共享调度器的增量目录交接检查；接口和媒体均由事件替身控制。"""

    def configure(self):
        """从当前配置派生下载、处理各一个名额；保留网络和容量配置。"""
        self.config = deepcopy(current_config())
        self.config['download']['threads'] = self.config['max_in_flight'] = 1
        self.interval = self.config['runtime']['cancel_poll_interval_seconds']
        self.process_prefix, self.download_prefix = 'tencent-process', 'tencent-download'

    def run_pipeline(self, source):
        """用真实 Discovery 展开 source 的两项目录，并接入生产 URL 调度器。"""
        owner = self
        class Log(MemoryJournal):
            """把生产日志中的等待队列通知转交测试观察器。"""
            def update(self, entry, stage=None, **fields):
                """更新 entry/stage/fields，转发阶段和终态用于测试。"""
                super().update(entry, stage, **fields)
                owner.record(entry['index'], fields.get('status') if fields.get('status') in ('completed','interrupted') else stage)
        batch = UrlBatch.__new__(UrlBatch)
        batch.config, batch.cancel, batch.auth_stop = self.config, self.cancel, Event()
        batch.journal, batch.interval = Log(), self.interval
        batch._job_steps = lambda task, entry:self.steps(task['index'])
        quality = self.config['download']['quality']
        values = [Request('series', str(i), quality, True, str(i)) for i in range(2)]
        def catalog(client, request):
            """用 client/request 接口签名推进受控目录；返回一个完整模拟正片任务。"""
            index = next(source)
            return [{'series_id':'series','video_id':str(index),'index':index,'title':str(index),'quality':quality}]
        with patch('local.playback.tasks.catalog', side_effect=catalog):
            batch.run([], Discovery(None, values, self.cancel))

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



class CatalogHandoffTests(unittest.TestCase):
    """真实目录去重事件的隔离检查，覆盖后续分组和迟到画质冲突。"""

    def test_late_membership_updates_completed_task_and_local_failure_continues(self):
        """已完成任务接收后续重复 URL 分组；另一个目录失败只计一次，不触发重复下载。"""
        from local.playback.errors import PlaybackError
        config = current_config()
        cancel = Event()
        quality = config['download']['quality']
        values = [Request('series','video',quality,True,str(i)) for i in range(3)]
        task = {'series_id':'series','video_id':'video','title':'fixture','quality':quality}
        batch = UrlBatch.__new__(UrlBatch)
        batch.config, batch.cancel, batch.auth_stop = config, cancel, Event()
        batch.journal, batch.interval = MemoryJournal(), config['runtime']['cancel_poll_interval_seconds']
        def steps(task, entry):
            """task/entry 为正式协议；直接返回可靠复用状态，不进入下载池。"""
            return {'status':'reused'}
            yield 'download', 0
        batch._job_steps = steps
        with patch('local.playback.tasks.catalog', side_effect=[[dict(task)],[dict(task)],PlaybackError('fixture failure')]):
            summary = batch.run([], Discovery(None, values, cancel))
        self.assertEqual((summary['total'],summary['reused'],summary['failed']), (2,1,1))
        self.assertEqual([group['id'] for group in batch.journal.entries[0]['groups']], ['0','1'])
        self.assertEqual(batch.journal.entries[0]['status'], 'reused')
        self.assertEqual(batch.journal.run['discovery']['status'], 'completed')

    def test_late_quality_conflict_does_not_mutate_handed_off_task(self):
        """后续重复画质冲突仍明确拒绝，已交接任务对象及分组不被工作线程改写。"""
        from local.playback.errors import PlaybackError
        config = current_config()
        quality = config['download']['quality']
        values = [Request('series','video',quality,True,'first'), Request('series','video',quality+1,True,'second')]
        tasks = [[{'series_id':'series','video_id':'video','series':'fixture','title':'fixture','quality':q}]
                 for q in (quality,quality+1)]
        source = Discovery(None, values, Event())
        with patch('local.playback.tasks.catalog', side_effect=tasks):
            first = source.advance()['tasks'][0]
            with self.assertRaisesRegex(PlaybackError, 'quality'):
                source.advance()
        self.assertEqual(len(first['groups']), 1)
        self.assertEqual(first['quality'], quality)
        self.assertEqual(source.position, 1)
