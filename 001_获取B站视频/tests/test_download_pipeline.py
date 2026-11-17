"""离线验证独立并发上限、字节队列、取消及两阶段媒体处理，不访问真实账号。"""

from collections import deque
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event, Lock, Thread, get_ident, enumerate as threads
import tempfile
import unittest
from unittest.mock import patch

import yaml

from bilibili_downloader import config, errors, metadata, runtime, scheduler, storage
from tests import CONFIG_PATH
from tests import test_batch_download as batch
from tests import test_merge_parts as merges


class PipelineTests(unittest.TestCase):
    """用可控阶段和同步事件验证调度边界；测试并发参数独立于用户当前配置。"""

    def setUp(self) -> None:
        """读取实际配置并覆盖测试所需的小容量及两阶段上限；无返回值。"""
        self.settings = replace(config.load_settings(CONFIG_PATH), threads=2, max_in_flight=2,
                                queue_capacity_gb=10 / 1024**3, cancel_poll_interval=0.01)

    def start(self, jobs: list[int], download, process, *, waiting=None, finishers=None, prepare=None):
        """在独立线程调度 jobs/download/process；可注入 waiting/finishers，返回线程、取消事件、终态及异常。"""
        cancel, outcomes, failures = Event(), {}, []

        def complete(job, result, error) -> None:
            """保存 job 的 result/error；返回 None。"""
            outcomes[job] = error if error is not None else result

        def run() -> None:
            """运行隔离流水线，捕获测试线程中的异常供主线程断言；无参数和返回值。"""
            try:
                scheduler.run_pipeline(jobs, self.settings, prepare or (lambda job: job), download, process, complete, cancel,
                                       downloaded_size=lambda result: result[1],
                                       waiting=waiting or (lambda *args: None),
                                       finishers=deque() if finishers is None else finishers)
            except BaseException as error:
                failures.append(error)

        thread = Thread(target=run, name='pipeline-test')
        thread.start()

        def cleanup() -> None:
            """在用例退出时取消仍运行的调度并等待回收；无参数和返回值。"""
            cancel.set()
            thread.join(10)

        self.addCleanup(cleanup)
        return thread, cancel, outcomes, failures

    def test_independent_limits_and_main_thread_callbacks(self) -> None:
        """下载和处理各达到自身上限并重叠执行；准备使用共享处理池，结算在调用线程，无参数和返回值。"""
        barrier, full_processing, advanced, prepared_all = Barrier(2), Event(), Event(), Event()
        guard, counts, maxima, prepared, results = Lock(), [0, 0], [0, 0], [], []
        main = get_ident()

        def prepare(job: int) -> int:
            """确认处理池线程并记录 job 的准备，返回原编号。"""
            self.assertNotEqual(get_ident(), main)
            prepared.append(job)
            if job == 3:
                prepared_all.set()
            return job

        def download(job: int, cancel: Event) -> tuple[int, int]:
            """同步首批 job 的下载并让后续下载等待处理池填满；cancel 为兼容参数，返回编号和大小。"""
            with guard:
                counts[0] += 1
                maxima[0] = max(maxima[0], counts[0])
            try:
                if job < 2:
                    barrier.wait(5)
                    self.assertTrue(prepared_all.wait(5))
                else:
                    self.assertTrue(full_processing.wait(5))
                    advanced.set()
                return job, 1
            finally:
                with guard:
                    counts[0] -= 1

        def process(value: tuple[int, int], cancel: Event) -> int:
            """对 value 统计处理并发，首批等待后续下载；cancel 为兼容参数，返回编号。"""
            with guard:
                counts[1] += 1
                maxima[1] = max(maxima[1], counts[1])
                if counts[1] == self.settings.max_in_flight:
                    full_processing.set()
            try:
                self.assertTrue(advanced.wait(5))
                return value[0]
            finally:
                with guard:
                    counts[1] -= 1

        def complete(job, result, error) -> None:
            """在主线程断言 job/result/error 并保存编号；无返回值。"""
            self.assertEqual(get_ident(), main)
            self.assertIsNone(error)
            self.assertEqual(job, result)
            results.append(result)

        scheduler.run_pipeline(list(range(6)), self.settings, prepare, download, process, complete, Event(),
                               downloaded_size=lambda value: value[1], waiting=lambda *args: None, finishers=deque())
        self.assertEqual(maxima, [self.settings.threads, self.settings.max_in_flight])
        self.assertEqual(sorted(prepared), list(range(6)))
        self.assertEqual(sorted(results), sorted(prepared))

    def test_finishers_share_processing_limit_without_stopping_downloads(self) -> None:
        """附加拼接收尾占满处理池时原始流仍下载，媒体后处理等待同一上限；无返回值。"""
        self.settings = replace(self.settings, max_in_flight=1)
        entered, downloaded, release = Event(), Event(), Event()
        processed, finished = [], []

        def finish() -> None:
            """阻塞一个模拟拼接，直到测试释放共享后处理槽；无参数和返回值。"""
            entered.set()
            self.assertTrue(release.wait(5))

        def download(job, cancel):
            """确认拼接已占槽后完成 job 的传输；cancel 为兼容参数，返回编号与大小。"""
            self.assertTrue(entered.wait(5))
            downloaded.set()
            return job, 1

        def process(value, cancel):
            """记录 value 的实际后处理派发；cancel 为兼容参数，返回编号。"""
            processed.append(value[0])
            return value[0]

        task = scheduler.PostTask(finish, finished.append)
        finishers = deque()

        def prepare(job):
            """先准备首个 job，再排入占用同一处理池的收尾，返回载荷。"""
            if job == 0:
                finishers.append(task)
            return job

        thread, cancel, outcomes, failures = self.start([0, 1], download, process, finishers=finishers, prepare=prepare)
        try:
            self.assertTrue(downloaded.wait(5))
            self.assertEqual(processed, [])
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(finished, [None])
        self.assertEqual(outcomes, {0: 0, 1: 1})

    def bounded_case(self, sizes: list[int], *, cancel_run: bool = False) -> None:
        """按 sizes 填满处理池和字节队列；cancel_run 选择取消验收，否则验收释放及全部完成，无返回值。"""
        self.settings = replace(self.settings, max_in_flight=1)
        processing, first_queued, blocked, release, prepared_all = Event(), Event(), Event(), Event(), Event()
        guard, started, states, processed = Lock(), [], {}, []

        def download(job, cancel):
            """让 job 在可控阶段传输，cancel 为取消事件；返回编号和实际流大小。"""
            if job == 0:
                self.assertTrue(prepared_all.wait(5))
            elif job == 1:
                self.assertTrue(processing.wait(5))
            elif job >= 2:
                self.assertTrue(first_queued.wait(5))
            with guard:
                started.append(job)
            return job, sizes[job]

        def waiting(job, status) -> None:
            """按 job/status 观察队列入场和两个被容量阻塞的下载名额；无返回值。"""
            states[job] = status
            if job == 1 and status == 'waiting_processing':
                first_queued.set()
            if all(states.get(index) == 'waiting_queue' for index in (2, 3)):
                blocked.set()

        def process(value, cancel):
            """首个 value 阻塞后处理，cancel 取消时抛出中断；返回已处理编号。"""
            if value[0] == 0:
                processing.set()
                while not release.wait(0.01):
                    errors.check_cancel(cancel)
            errors.check_cancel(cancel)
            processed.append(value[0])
            return value[0]

        def prepare(job):
            """在首个后处理开始前准备有界下载缓存；返回 job 载荷。"""
            if job == 3:
                prepared_all.set()
            return job

        thread, cancel, outcomes, failures = self.start(list(range(len(sizes))), download, process, waiting=waiting, prepare=prepare)
        try:
            self.assertTrue(blocked.wait(5))
            with guard:
                self.assertEqual(set(started), {0, 1, 2, 3})
            self.assertEqual(states[1], 'waiting_processing')
            self.assertEqual(processed, [])
            if cancel_run:
                cancel.set()
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        if cancel_run:
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], errors.DownloadCancelled)
            self.assertNotIn(4, outcomes)
            self.assertTrue(any(isinstance(outcome, errors.DownloadCancelled) for outcome in outcomes.values()))
        else:
            self.assertEqual(failures, [])
            self.assertEqual(processed[:2], [0, 1])
            self.assertEqual(sorted(processed), list(range(len(sizes))))
            self.assertEqual(outcomes, {index: index for index in range(len(sizes))})
        self.assertFalse(any(thread.name.startswith(('bilibili-download', 'bilibili-process')) for thread in threads()))

    def test_queue_capacity_and_release(self) -> None:
        """验证队列按字节背压，出队后继续下载和处理；无参数和返回值。"""
        self.bounded_case([6] * 5)

    def test_one_oversized_part_enters_empty_queue(self) -> None:
        """验证一个超容量分 P 可独占空队列并最终完成；无参数和返回值。"""
        self.bounded_case([1, 12, 1, 1, 1])

    def test_cancel_processing_ready_and_admission_queues(self) -> None:
        """验证取消同时清理处理、入队及等待处理阶段，保留未派发任务；无参数和返回值。"""
        self.bounded_case([6] * 5, cancel_run=True)

    def test_cancel_active_downloads(self) -> None:
        """验证运行中的下载协作退出，后处理和后续任务不启动；无参数和返回值。"""
        entered = Event()

        def download(job, cancel):
            """阻塞 job 下载直到 cancel，再抛出中断；无正常返回值。"""
            entered.set()
            while not cancel.wait(0.01):
                pass
            errors.check_cancel(cancel)

        thread, cancel, outcomes, failures = self.start(list(range(5)), download, lambda value, event: self.fail('不应后处理'))
        self.assertTrue(entered.wait(5))
        cancel.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], errors.DownloadCancelled)
        self.assertLessEqual(len(outcomes), self.settings.threads * 2)
        self.assertTrue(all(isinstance(outcome, errors.DownloadCancelled) for outcome in outcomes.values()))

    def test_failures_do_not_stop_other_tasks(self) -> None:
        """下载和后处理各失败一项时，其余任务正常完成；无参数和返回值。"""
        def download(job, cancel):
            """让 job=1 下载失败；cancel 为兼容参数，其他编号返回零字节结果。"""
            if job == 1:
                raise errors.ToolError('传输失败')
            return job, 0

        def process(value, cancel):
            """让 value 编号为 2 的处理失败；cancel 为兼容参数，其他编号正常返回。"""
            if value[0] == 2:
                raise errors.ToolError('封装失败')
            return value[0]

        thread, cancel, outcomes, failures = self.start(list(range(5)), download, process)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual([outcomes[index] for index in (0, 3, 4)], [0, 3, 4])
        self.assertTrue(all(isinstance(outcomes[index], errors.ToolError) for index in (1, 2)))


class PipelineConfigTests(unittest.TestCase):
    """验证两池和容量配置不兜底、不接受非法数值。"""

    def test_settings_values_and_validation(self) -> None:
        """读取临时覆盖配置，验证缺项、整数上限和字节容量边界；无参数和返回值。"""
        original = yaml.safe_load(CONFIG_PATH.read_text(encoding='utf-8-sig'))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config.yaml'
            for key, invalid in (('max_in_flight', [None, True, 0, -1, 1.5, '4']),
                                 ('queue_capacity_gb', [None, True, 0, -1, float('inf'), float('nan'), 1e308, 0.1 / 1024**3])):
                for value in invalid:
                    data = yaml.safe_load(yaml.safe_dump(original))
                    target = data if key == 'max_in_flight' else data['download']
                    target[key] = value
                    path.write_text(yaml.safe_dump(data), encoding='utf-8')
                    with self.subTest(key=key, value=value), self.assertRaisesRegex(errors.ToolError, key):
                        config.load_settings(path)
                del target[key]
                path.write_text(yaml.safe_dump(data), encoding='utf-8')
                with self.subTest(missing=key), self.assertRaisesRegex(errors.ToolError, key):
                    config.load_settings(path)
            original['download'].update(threads=3, queue_capacity_gb=1 / 1024**3)
            original['max_in_flight'] = 2
            path.write_text(yaml.safe_dump(original), encoding='utf-8')
            settings = config.load_settings(path)
            self.assertEqual((settings.threads, settings.max_in_flight, settings.queue_capacity_gb), (3, 2, 1 / 1024**3))


class PipelineIntegrationTests(unittest.TestCase):
    """使用真实记录、流水线和本地假媒体验证阶段交接与并发索引。"""

    def test_transaction_snapshot_read_blocks_atomic_replacement(self) -> None:
        """事务备份保持真实读句柄时，原子替换必须等待读完，避免 Windows 拒绝访问；无参数和返回值。"""
        from concurrent.futures import ThreadPoolExecutor

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'video_info.json'
            path.write_text('{"state":"old"}', encoding='utf-8')
            reading, release, writing, replacing = Event(), Event(), Event(), Event()
            original_open, original_replace = Path.open, Path.replace

            def open_record(value, *args, **kwargs):
                """以 args/kwargs 打开 value，备份读取时保持真实二进制句柄；返回文件流。"""
                stream = original_open(value, *args, **kwargs)
                mode = args[0] if args else kwargs.get('mode', 'r')
                if value == path and mode == 'rb':
                    original_read = stream.read

                    def read(*read_args, **read_kwargs):
                        """按 read_args/read_kwargs 读取备份并等待测试放行，返回原始字节。"""
                        data = original_read(*read_args, **read_kwargs)
                        reading.set()
                        self.assertTrue(release.wait(5))
                        return data

                    stream.read = read
                return stream

            def replace_record(value, target):
                """观察 value 替换 target 的实际调用时机，返回原始替换结果。"""
                if target == path:
                    replacing.set()
                return original_replace(value, target)

            def write() -> None:
                """通过真实原子写入更新正在备份的记录；无参数和返回值。"""
                writing.set()
                metadata.atomic_text(path, '{"state":"new"}')

            with patch.object(Path, 'open', open_record), patch.object(Path, 'replace', replace_record), ThreadPoolExecutor(max_workers=2) as pool:
                reader = pool.submit(storage.apply_changes, root, [], [path], lambda: None)
                try:
                    self.assertTrue(reading.wait(5))
                    writer = pool.submit(write)
                    self.assertTrue(writing.wait(5))
                    self.assertFalse(replacing.wait(0.1), '备份读句柄关闭前不得尝试替换记录')
                finally:
                    release.set()
                reader.result(timeout=5)
                writer.result(timeout=5)
            self.assertEqual(metadata.read_json(path), {'state': 'new'})

    def test_next_part_downloads_while_first_muxes(self) -> None:
        """各池只有一个名额时，首 P 封装期间下一 P 仍能完成下载；无参数和返回值。"""
        engine = batch.BatchEngineTests()
        engine.setUp()
        self.addCleanup(engine.doCleanups)
        engine.settings = replace(engine.settings, threads=1, max_in_flight=1)
        self.enterContext(patch.object(config, 'load_settings', return_value=engine.settings))
        source = engine.ids[0]
        engine.counts[source] = 2
        entered, downloaded = Event(), Event()
        original_transfer, original_mux = engine.transfer, engine.mux

        def transfer(filename, info, hooks):
            """以 filename/info/hooks 完成模拟传输，第二 P 确认首 P 正在封装；返回传输标记。"""
            if info['id'].endswith('_p2'):
                self.assertTrue(entered.wait(5))
                downloaded.set()
            return original_transfer(filename, info, hooks)

        def mux(*args):
            """首个 args 对应分 P 封装时等待下一 P 下载；返回隔离成品路径。"""
            if not entered.is_set():
                entered.set()
                self.assertTrue(downloaded.wait(5))
            return original_mux(*args)

        engine.transfer = transfer
        with patch.object(batch.media, 'mux_streams', side_effect=mux):
            self.assertEqual(engine.run_list([{'url': source}]), 0, engine.stderr.getvalue())
        self.assertEqual(sum(engine.calls.values()), 2)
        self.assertTrue(all(row['status'] == 'completed' for row in engine.read_log()['runs'][-1]['entries']))

    def test_parallel_merges_preserve_files_and_results(self) -> None:
        """同一合并目录同时处理不同序列时保留全部成品和结果，探测在索引锁外重叠；无参数和返回值。"""
        from concurrent.futures import ThreadPoolExecutor

        fixture = merges.MergeGroupTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        record = fixture.make_record()
        barrier = Barrier(2)

        def probe(executable, path):
            """在 executable/path 的模拟探测处同步两个拼接，返回本地媒体属性。"""
            barrier.wait(5)
            return {'duration_seconds': 1, 'errors': []}

        with patch.object(merges.metadata, 'probe_media', side_effect=probe), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(merges.merge_service.merge_parts, fixture.settings, 'ffmpeg', record, parts,
                                   quality=merges.models.LEGACY_QUALITY) for parts in ([1, 2], [2, 3])]
            outputs = [future.result(timeout=10) for future in futures]
        self.assertTrue(all(path.is_file() for path in outputs))
        index = fixture.merged_index(record)
        self.assertEqual(len(index.data['files']), 2)
        results = merges.MergedVideoInfo.for_results(record).data['merge_results']
        self.assertEqual(len(results), 2)
        self.assertTrue(all(item['status'] == 'completed' for item in results))

    def test_cancelled_queue_keeps_streams_and_reruns_successfully(self) -> None:
        """真实队列取消后保留完整原始流、无虚假成功，重跑能生成全部成品；无参数和返回值。"""
        engine = batch.BatchEngineTests()
        engine.setUp()
        self.addCleanup(engine.doCleanups)
        engine.settings = replace(engine.settings, threads=1, max_in_flight=3,
                                  queue_capacity_gb=1 / 1024**3)
        self.enterContext(patch.object(config, 'load_settings', return_value=engine.settings))
        source = engine.ids[0]
        engine.counts[source] = 5
        from bilibili_downloader import workflow
        original = workflow.run_pipeline
        entered = Event()

        def mux(*args):
            """在 args 所属媒体开始封装时阻塞，等待队列回调触发真实取消；无正常返回值。"""
            entered.set()
            controller = runtime.current()
            if not controller.event.wait(10):
                raise TimeoutError("等待队列取消超时")
            errors.check_cancel(controller.event)

        def pipeline(*args, **kwargs):
            """为 args/kwargs 包装等待回调，第三 P 等待容量时取消；返回原调度器结果。"""
            original_waiting = kwargs['waiting']

            def waiting(job, status) -> None:
                """按 job/status 保存等待状态，并在第三 P 被阻塞时请求取消；无返回值。"""
                original_waiting(job, status)
                if job.target.part == 3 and status == 'waiting_queue':
                    self.assertTrue(entered.is_set())
                    runtime.current().cancel()

            kwargs['waiting'] = waiting
            return original(*args, **kwargs)

        with patch.object(workflow, 'run_pipeline', side_effect=pipeline), patch.object(batch.media, 'mux_streams', side_effect=mux):
            self.assertEqual(engine.run_list([{'url': source}]), 130, engine.stderr.getvalue())
        streams = [path for path in engine.settings.output_dir.rglob('*.mp4') if '.streams' in path.parts]
        self.assertEqual(len(streams), 3)
        self.assertTrue(all(path.read_bytes() for path in streams))
        entries = engine.read_log()['runs'][-1]['entries']
        self.assertTrue(all(row['status'] in {'interrupted', 'skipped'} for row in entries))
        self.assertEqual(engine.run_list([{'url': source}]), 0, engine.stderr.getvalue())
        entries = engine.read_log()['runs'][-1]['entries']
        self.assertTrue(all(row['status'] == 'completed' for row in entries))
        self.assertEqual(sum(row['kind'] == '下载' for row in entries), 5)
        self.assertFalse(any(path.exists() for path in streams))

    def test_same_target_parallel_merge_reuses_first_publication(self) -> None:
        """关闭覆盖时，并发处理同一目标只发布一次，后完成者保留先发布文件并记录复用；无参数和返回值。"""
        from concurrent.futures import ThreadPoolExecutor

        fixture = merges.MergeGroupTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        record = fixture.make_record()
        settings = replace(fixture.settings, overwrite_merged=False)
        barrier = Barrier(2)

        def probe(executable, path):
            """在 executable/path 的模拟探测处同步相同目标，使两者发布前都看不到成品；返回媒体属性。"""
            barrier.wait(5)
            return {'duration_seconds': 1, 'errors': []}

        with patch.object(merges.metadata, 'probe_media', side_effect=probe), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(merges.merge_service.merge_parts, settings, 'ffmpeg', record, [1, 2],
                                   quality=merges.models.LEGACY_QUALITY) for _ in range(2)]
            outputs = [future.result(timeout=10) for future in futures]
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(len(fixture.merged_index(record).data['files']), 1)
        results = merges.MergedVideoInfo.for_results(record).data['merge_results']
        self.assertEqual(sorted(item['status'] for item in results), ['completed', 'reused'])
