"""以阻塞目录和合成媒体验证增量发现，不读取真实账号、不访问外部网络。"""
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import Event, Lock, Thread, current_thread, enumerate as enumerate_threads
import unittest
from unittest.mock import patch

import yaml
from qsv_local.runtime import check_cancel
from qsv_local.playback.coordination import StageControls
from qsv_local.playback.scheduler import schedule

class IncrementalDiscoveryTests(unittest.TestCase):
    """真实共享调度器的增量目录交接检查；接口和媒体均由事件替身控制。"""

    def configure(self):
        """从当前 YAML 派生两个单名额池，容量等其余配置继续读取当前文件。"""
        self.config = yaml.safe_load((Path(__file__).resolve().parents[1]/'configs/config.yaml').read_text(encoding='utf-8-sig'))
        self.config['download']['threads'] = self.config['max_in_flight'] = 1
        self.interval = self.config['runtime']['cancel_poll_interval_seconds']
        self.process_prefix, self.download_prefix = 'iqiyi-process', 'iqiyi-download'

    def run_pipeline(self, source):
        """通过 production schedule 的目录交接回调登记 source 返回的任务。"""
        controls = StageControls(self.config, self.cancel)
        def admit(event):
            """event 为目录中的任务编号，None 为耗尽；返回单集条目和阶段生成器。"""
            return [] if event is None else [(event, self.steps(event))]
        def update(index, status, value):
            """将 index 的生产 status/value 转为测试观察状态。"""
            self.record(index, value['status'] if status == 'done' else status, value)
        schedule([], self.config, controls, update, discovery=source, admit=admit)

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



class ApplicationDiscoveryTests(unittest.TestCase):
    """验证正式入口采用增量计划，公开计划模式保留原有无会话行为。"""

    def test_download_entry_admits_each_catalog_without_eager_build_plan(self):
        """通过真实入口、计划迭代器及调度器交接两项任务，全部 I/O 和媒体均使用替身。"""
        from threading import RLock
        from unittest.mock import MagicMock
        from contextlib import nullcontext
        from qsv_local.playback.application import run_downloads
        from qsv_local.playback.listing import task_options
        root = Path(__file__).resolve().parents[1]
        raw = yaml.safe_load((root/'configs/config.yaml').read_text(encoding='utf-8-sig'))
        raw['logging']['open_browser'] = False
        playback = yaml.safe_load((root/'configs/playback.yaml').read_text(encoding='utf-8-sig'))
        configured = task_options({'main_config':root/'configs/config.yaml',
                                   'download_list':(root/'configs'/playback['download_list']).resolve()})[0]
        options = [{'index':i,'quality':configured['quality'],'all_parts':True,'note':''} for i in range(2)]
        videos = [{'tvid':str(i),'episode':i+1,'title':'fixture','episode_title':str(i),'album_id':'fixture'} for i in range(2)]
        journal = MagicMock()
        journal.run, journal.lock, journal.url = {}, RLock(), 'fixture'
        journal.__enter__.return_value = journal
        journal.add_episode.side_effect = lambda *args:{}
        def steps(*args):
            """args 沿用正式单集调用协议；无媒体传输，直接返回复用结果。"""
            return {'status':'reused','output':'fixture','media':{}}
            yield 'download', 0
        with patch('qsv_local.playback.application.read_profile', return_value={'main_config':root/'configs/config.yaml'}), \
                patch('qsv_local.playback.application.media_config', return_value=raw), \
                patch('qsv_local.playback.application.output_lock', return_value=nullcontext()), \
                patch('qsv_local.playback.application.Journal', return_value=journal), \
                patch('qsv_local.playback.application.consolidate_output', return_value={}), \
                patch('qsv_local.playback.application.task_options', return_value=options), \
                patch('qsv_local.playback.application.linked_videos', side_effect=[[video] for video in videos]), \
                patch('qsv_local.playback.application.expand_episodes', side_effect=lambda profile,video,whole,cancel=None:[video]), \
                patch('qsv_local.playback.application.job_steps', side_effect=steps), \
                patch('qsv_local.playback.application.atomic_json'), \
                patch('qsv_local.playback.application.build_plan', side_effect=AssertionError('must not eagerly build all catalogs')):
            report, location = run_downloads(root/'configs/playback.yaml')
        self.assertEqual((report['total_episodes'], report['reused'], report['failed']), (2,2,0))
        self.assertEqual(journal.run['catalog_status'], 'ready')
        self.assertEqual(journal.run['catalog_processed'], len(options))
