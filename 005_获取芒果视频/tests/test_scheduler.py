"""通过同步事件验证独立线程池、按字节背压、认证停止及取消结算，不访问外部网络。"""

from copy import deepcopy
from threading import Barrier, Event, Lock, Thread, enumerate as threads, get_ident

import yaml

from local.client import check_cancel
from local.configuration import read_config, read_yaml
from local.errors import AuthenticationError, Cancelled, ToolError
from local.scheduler import run_pipeline
from tests.stages import staged
from tests.test_workflow import BaseCase, ROOT


class SchedulerTests(BaseCase):
    """从实际配置构建小容量隔离场景，验证调度行为而非生产配置的固定数值。"""

    def setUp(self):
        """读取配置后临时设置两池及字节容量，便于用小载荷触发背压；无参数/返回值。"""
        super().setUp()
        self.config["download"].update(threads=2, queue_capacity_gb=10 / 1024**3)
        self.config["max_in_flight"] = 2
        self.config["runtime"]["cancel_poll_interval_seconds"] = 0.01

    def start(self, jobs, download, process, waiting=None, steps=None):
        """后台调度 jobs/download/process，可注入 waiting；返回线程、取消事件、终态映射及批次异常列表。"""
        cancel, outcomes, failures = Event(), {}, []
        self.cancel = cancel

        def complete(job, status, error):
            """保存 job 的 status/error；无返回值。"""
            outcomes[job] = error if error is not None else status

        def run():
            """执行隔离调度并捕获批次异常供测试线程断言；无参数/返回值。"""
            try:
                run_pipeline(jobs, self.config, steps or staged(download, process), complete, cancel, Event(),
                             downloaded_size=lambda value: value[1], waiting=waiting or (lambda *args: None))
            except BaseException as error:
                failures.append(error)

        thread = Thread(target=run, name="scheduler-test")
        thread.start()

        def cleanup():
            """取消残留任务并等待线程退出，避免失败用例遗留工作者；无参数/返回值。"""
            cancel.set()
            thread.join(10)

        self.addCleanup(cleanup)
        return thread, cancel, outcomes, failures

    def assert_reclaimed(self, thread):
        """断言 thread 和本工具的两个线程池都已退出；无返回值。"""
        self.assertFalse(thread.is_alive())
        self.assertFalse(any(item.name.startswith(("mgtv-download", "mgtv-process")) for item in threads()))

    def test_independent_limits_and_calling_thread_callbacks(self):
        """两池分别达到自身上限且交叠，迭代及日志/结算回调保持在调用线程；无参数/返回值。"""
        barrier, processing_full, advanced, prepared_all = Barrier(2), Event(), Event(), Event()
        guard, active, maxima, completed = Lock(), [0, 0], [0, 0], []
        caller = get_ident()

        def jobs():
            """在调用线程按需产生六个隔离任务编号；无参数；逐项返回编号。"""
            for job in range(6):
                self.assertEqual(get_ident(), caller)
                yield job

        def download(job):
            """同步首批 job 的下载，让后续下载与占满的处理池交叠；返回编号及字节数。"""
            with guard:
                active[0] += 1
                maxima[0] = max(maxima[0], active[0])
            try:
                if job < barrier.parties:
                    barrier.wait(5)
                    self.assertTrue(prepared_all.wait(5))
                else:
                    self.assertTrue(processing_full.wait(5))
                    advanced.set()
                return job, 1
            finally:
                with guard:
                    active[0] -= 1

        def process(value):
            """对 value 占用处理槽直到观察到下载补位；返回编号字符串。"""
            with guard:
                active[1] += 1
                maxima[1] = max(maxima[1], active[1])
                if active[1] == self.config["max_in_flight"]:
                    processing_full.set()
            try:
                self.assertTrue(advanced.wait(5))
                return str(value[0])
            finally:
                with guard:
                    active[1] -= 1

        def complete(job, status, error):
            """在调用线程验证 job/status/error 并登记完成；无返回值。"""
            self.assertEqual(get_ident(), caller)
            self.assertIsNone(error)
            self.assertEqual(status, str(job))
            completed.append(job)

        def waiting(job, status):
            """验证 job 的 status 通知在调用线程执行；无返回值。"""
            self.assertEqual(get_ident(), caller)

        def steps(job):
            """先预解析有界缓存再放行下载，避免把处理池独立于解析当作测试前提。"""
            if job == 3:
                prepared_all.set()
            return (yield from staged(download, process)(job))

        run_pipeline(jobs(), self.config, steps, complete, Event(), Event(),
                     downloaded_size=lambda value: value[1], waiting=waiting)
        self.assertEqual(maxima, [self.config["download"]["threads"], self.config["max_in_flight"]])
        self.assertEqual(sorted(completed), list(range(6)))

    def bounded_case(self, sizes, cancel_run=False):
        """按 sizes 构建满处理池、队列及入队等待；cancel_run 选择取消，否则释放后验收完成；无返回值。"""
        self.config["max_in_flight"] = 1
        processing, first_queued, blocked, release, all_prepared = Event(), Event(), Event(), Event(), Event()
        guard, started, states, processed = Lock(), [], {}, []

        def download(job):
            """按受控先后顺序下载 job；返回编号和 sizes 中的实际字节数。"""
            if job == 0:
                self.assertTrue(all_prepared.wait(5))
            elif job == 1:
                self.assertTrue(processing.wait(5))
            elif job >= 2:
                self.assertTrue(first_queued.wait(5))
            with guard:
                started.append(job)
            return job, sizes[job]

        def waiting(job, status):
            """观察 job/status，标记队列已有首项及两个下载名额等待入队；无返回值。"""
            states[job] = status
            if job == 1 and status == "等待后处理":
                first_queued.set()
            if all(states.get(index) == "等待队列容量" for index in (2, 3)):
                blocked.set()

        def process(value):
            """首个 value 阻塞处理至测试释放或取消；返回编号字符串。"""
            if value[0] == 0:
                processing.set()
                while not release.wait(self.config["runtime"]["cancel_poll_interval_seconds"]):
                    check_cancel(self.cancel)
            check_cancel(self.cancel)
            processed.append(value[0])
            return str(value[0])

        def job_steps(job):
            """在首个后处理占槽前准备有界下载缓存；job 为测试编号，返回终态。"""
            if job == 3:
                all_prepared.set()
            return (yield from staged(download, process)(job))

        thread, cancel, outcomes, failures = self.start(range(len(sizes)), download, process, waiting, steps=job_steps)
        try:
            self.assertTrue(blocked.wait(5))
            with guard:
                self.assertEqual(set(started), {0, 1, 2, 3})
            self.assertEqual(states[1], "等待后处理")
            self.assertEqual(processed, [])
            if cancel_run:
                cancel.set()
        finally:
            release.set()
            thread.join(10)
        self.assert_reclaimed(thread)
        if cancel_run:
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], Cancelled)
            self.assertEqual(set(outcomes), {0, 1, 2, 3})
            self.assertTrue(all(isinstance(outcomes[index], Cancelled) for index in (1, 2, 3)))
        else:
            self.assertEqual(failures, [])
            self.assertEqual(processed[:2], [0, 1])
            self.assertEqual(sorted(processed), list(range(len(sizes))))
            self.assertEqual(outcomes, {index: str(index) for index in range(len(sizes))})

    def test_capacity_backpressure_and_release(self):
        """按字节容量暂停补位，出队后恢复下载并保持先进先出；无参数/返回值。"""
        self.bounded_case([6] * 5)

    def test_exact_capacity_and_single_oversized_episode(self):
        """恰好达到容量以及单集超限均能入空队列，后续任务不能绕过背压；无参数/返回值。"""
        for size in (10, 12):
            with self.subTest(size=size):
                self.bounded_case([1, size, 1, 1, 1])

    def test_cancel_processing_ready_and_admission(self):
        """取消同时结算处理中、等待处理和等待入队的任务，未接纳任务不启动；无参数/返回值。"""
        self.bounded_case([6] * 5, cancel_run=True)

    def test_cancel_active_downloads(self):
        """活动下载协作取消，不启动后处理或后续任务，所有已接纳任务有终态；无参数/返回值。"""
        entered, guard, started = Event(), Lock(), []

        def download(job):
            """阻塞 job 的下载直至取消；无正常返回值。"""
            with guard:
                started.append(job)
                if len(started) == self.config["download"]["threads"]:
                    entered.set()
            self.cancel.wait(5)
            check_cancel(self.cancel)
            self.fail("应当取消")

        thread, cancel, outcomes, failures = self.start(range(5), download, lambda value: self.fail("不应后处理"))
        self.assertTrue(entered.wait(5))
        cancel.set()
        thread.join(10)
        self.assert_reclaimed(thread)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], Cancelled)
        self.assertTrue(set(started) <= set(outcomes))
        self.assertLessEqual(len(outcomes), self.config["download"]["threads"] * 2)
        self.assertTrue(all(isinstance(value, Cancelled) for value in outcomes.values()))

    def test_completed_terminal_status_survives_cancellation(self):
        """处理完成后立即取消仍保留已完成终态；无参数/返回值。"""
        cancel, outcomes = Event(), {}

        def process(value):
            """模拟 value 已发布后请求取消；返回成功终态。"""
            cancel.set()
            return "成功"

        with self.assertRaises(Cancelled):
            run_pipeline([0], self.config, staged(lambda job: (job, 1), process),
                         lambda job, status, error: outcomes.update({job: error or status}), cancel, Event(),
                         downloaded_size=lambda value: value[1], waiting=lambda *args: None)
        self.assertEqual(outcomes, {0: "成功"})

    def test_authentication_stops_downloads_but_drains_ready(self):
        """认证失效后不派发新下载，已下载的等待任务仍能完成；无参数/返回值。"""
        self.config["download"]["threads"] = 1
        self.config["max_in_flight"] = 2
        auth_stop, release, outcomes, started = Event(), Event(), {}, []

        def download(job):
            """记录 job；第三项模拟认证失效并释放处理槽，其余返回载荷。"""
            started.append(job)
            if job == 2:
                auth_stop.set()
                release.set()
                raise AuthenticationError("票据过期")
            return job, 1

        def process(value):
            """首个 value 等待认证失败后完成，返回编号字符串。"""
            if value[0] == 0:
                self.assertTrue(release.wait(5))
            return str(value[0])

        run_pipeline(range(5), self.config, staged(download, process),
                     lambda job, status, error: outcomes.update({job: error or status}), Event(), auth_stop,
                     downloaded_size=lambda value: value[1], waiting=lambda *args: None)
        self.assertEqual(started, [0, 1, 2])
        self.assertEqual([outcomes[index] for index in (0, 1)], ["0", "1"])
        self.assertIsInstance(outcomes[2], AuthenticationError)

    def test_individual_failures_and_reuse_do_not_stop_batch(self):
        """下载/处理失败分别结算且继续，复用任务无需入队或后处理；无参数/返回值。"""
        processed = []

        def download(job):
            """对 job 生成正常载荷、下载错误或复用终态。"""
            if job == 1:
                raise ToolError("下载失败")
            return "复用" if job == 3 else (job, 1)

        def process(value):
            """对 value 模拟单项处理错误，其余返回编号字符串。"""
            processed.append(value[0])
            if value[0] == 2:
                raise ToolError("封装失败")
            return str(value[0])

        thread, _, outcomes, failures = self.start(range(5), download, process)
        thread.join(10)
        self.assert_reclaimed(thread)
        self.assertEqual(failures, [])
        self.assertEqual([outcomes[index] for index in (0, 3, 4)], ["0", "复用", "4"])
        self.assertTrue(all(isinstance(outcomes[index], ToolError) for index in (1, 2)))
        self.assertNotIn(3, processed)

    def test_catalog_failure_cancels_accepted_downloads(self):
        """增量目录发生批次异常时回收已接纳下载，未接纳任务不启动；无参数/返回值。"""
        cancel, outcomes = Event(), {}

        def jobs():
            """产生首个任务后模拟目录异常；无参数；返回首项编号后抛出错误。"""
            yield 0
            raise ToolError("目录异常")

        def download(job):
            """让已接纳的 job 等待批次取消；无正常返回值。"""
            cancel.wait(5)
            check_cancel(cancel)
            self.fail("应当取消")

        with self.assertRaisesRegex(ToolError, "目录异常"):
            run_pipeline(jobs(), self.config, staged(download, lambda value: self.fail("不应后处理")),
                         lambda job, status, error: outcomes.update({job: error or status}), cancel, Event(),
                         downloaded_size=lambda value: value[1], waiting=lambda *args: None)
        self.assertTrue(cancel.is_set())
        self.assertEqual(set(outcomes), {0})
        self.assertIsInstance(outcomes[0], Cancelled)
        self.assertFalse(any(item.name.startswith(("mgtv-download", "mgtv-process")) for item in threads()))


class SchedulerConfigTests(BaseCase):
    """验证主配置的两池参数和容量，不将生产值写死在断言中。"""

    def test_capacity_and_worker_validation(self):
        """校验容量缺失、非法/溢出/不足一字节及两池并发值，合法配置按原值读取；无参数/返回值。"""
        original = read_yaml(ROOT / "configs" / "config.yaml")
        for key in ("output_path", "work_dir", "ffmpeg_path", "native_profile", "default_list"):
            original[key] = str(self.config[key])
        original["auth"]["user_data"] = str(self.config["auth"]["user_data"])
        path = self.root / "config.yaml"
        cases = [("download", "queue_capacity_gb", [None, True, 0, -1, "20", float("inf"), float("nan"), 1e308, 0.1 / 1024**3]),
                 ("download", "threads", [None, True, 0, -1, 1.5, "5"]),
                 (None, "max_in_flight", [None, True, 0, -1, 1.5, "4"])]
        for section, field, invalid_values in cases:
            for value in invalid_values:
                data = deepcopy(original)
                (data[section] if section else data)[field] = value
                path.write_text(yaml.safe_dump(data), encoding="utf-8")
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ToolError, field):
                    read_config(path)
        data = deepcopy(original)
        del data["download"]["queue_capacity_gb"]
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with self.assertRaisesRegex(ToolError, "download.queue_capacity_gb"):
            read_config(path)
        data["download"].update(threads=3, queue_capacity_gb=1 / 1024**3)
        data["max_in_flight"] = 2
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        loaded = read_config(path)
        self.assertEqual(loaded["download"]["threads"], data["download"]["threads"])
        self.assertEqual(loaded["download"]["queue_capacity_gb"], data["download"]["queue_capacity_gb"])
        self.assertEqual(loaded["max_in_flight"], data["max_in_flight"])
        data["pipeline"]["max_in_flight"] = data.pop("max_in_flight")
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with self.assertRaisesRegex(ToolError, "顶层 max_in_flight"):
            read_config(path)
