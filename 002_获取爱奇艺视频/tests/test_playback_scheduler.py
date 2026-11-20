"""隔离验证链接下载、等待队列和后处理的调度边界，不访问真实账号。"""

from copy import deepcopy
from pathlib import Path
from threading import Event, Lock, Thread
import unittest

import yaml

from qsv_local.configuration import validate_concurrency
from qsv_local.playback.coordination import StageControls
from qsv_local.playback.scheduler import schedule


class PlaybackSchedulerTests(unittest.TestCase):
    """使用当前配置副本和受控生成器验证并发与队列行为。"""

    def setUp(self):
        """加载实际主配置并为并发测试创建受控上限；无返回值。"""
        root = Path(__file__).resolve().parents[1]
        self.config = yaml.safe_load((root / "configs/config.yaml").read_text(encoding="utf-8"))
        self.config["download"]["threads"] = 2
        self.config["download"]["queue_capacity_gb"] = 10 / 1024**3
        self.config["max_in_flight"] = 1

    def test_validation_rejects_invalid_concurrency(self):
        """下载数、容量及在途数缺失或无效时明确报错；无返回值。"""
        validate_concurrency(self.config)
        for section, key, values in (("download", "threads", [0, True, 1.5, None]),
                                     ("download", "queue_capacity_gb", [0, True, float("inf"), None]),
                                     (None, "max_in_flight", [0, False, 1.5, None])):
            for value in values:
                config = deepcopy(self.config)
                target = config[section] if section else config
                target[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    validate_concurrency(config)

    def test_downloads_continue_while_processing_and_queue_applies_backpressure(self):
        """处理首集时继续下载，队列满则暂停派发，释放后全部完成；无返回值。"""
        controls = StageControls(self.config)
        release = Event()
        processing = Event()
        waiting = Event()
        prepared = Event()
        started, states, guard = [], {}, Lock()

        def steps(index):
            """模拟 index 的下载和后处理，下载产生 8 字节，返回完成结果。"""
            if index == 3:
                prepared.set()
            yield "download", 0
            if index == 0 and not prepared.wait(5):
                raise TimeoutError("预解析未完成")
            with guard:
                started.append(index)
            if index and not processing.wait(5):
                raise TimeoutError("首集未开始后处理")
            yield "ready", 8
            if index == 0:
                processing.set()
                if not release.wait(5):
                    raise TimeoutError("测试等待释放后处理超时")
            return {"status": "completed", "index": index}

        def update(item, status, value):
            """记录 item 的 status 和 value；观察到队列阻塞时发出通知，无返回值。"""
            with guard:
                states[item] = status
                if list(states.values()).count("waiting_queue") >= 1 and "waiting_processing" in states.values():
                    waiting.set()

        jobs = [(i, steps(i)) for i in range(6)]
        errors = []

        def run():
            """运行受控队列并保存异常；无返回值。"""
            try:
                schedule(jobs, self.config, controls, update)
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=run)
        thread.start()
        try:
            self.assertTrue(processing.wait(5))
            self.assertTrue(waiting.wait(5))
            with guard:
                self.assertLess(len(started), len(jobs))
                self.assertEqual(states[0], "processing")
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertTrue(all(states[i] == "done" for i in range(len(jobs))))

    def test_download_thread_limit(self):
        """下载工作并发达到配置上限时，其他剧集等待派发；无返回值。"""
        controls = StageControls(self.config)
        release, both_started = Event(), Event()
        started, guard, statuses = [], Lock(), {}

        def steps(index):
            """模拟 index 下载并等待 release；返回合成完成结果。"""
            yield "download", 0
            with guard:
                started.append(index)
                if len(started) == self.config["download"]["threads"]:
                    both_started.set()
            if not release.wait(5):
                raise TimeoutError("测试下载未释放")
            yield "ready", 1
            return {"status": "completed"}

        def update(item, status, value):
            """记录 item 的 status，value 为可选结果；无返回值。"""
            statuses[item] = status

        thread = Thread(target=schedule, args=([(i, steps(i)) for i in range(3)], self.config, controls, update))
        thread.start()
        try:
            self.assertTrue(both_started.wait(5))
            with guard:
                self.assertEqual(len(started), self.config["download"]["threads"])
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(all(statuses[i] == "done" for i in range(3)))

    def test_downloads_keep_filling_while_processing_is_slow(self):
        """后处理长期占用时仍持续准备并下载后续剧集，处理总数不超过配置；无返回值。"""
        self.config["max_in_flight"] = 3
        self.config["download"]["queue_capacity_gb"] = 100 / 1024**3
        controls = StageControls(self.config)
        release, processing_busy, downloaded = Event(), Event(), Event()
        lock, active, peak, processing_count, started, statuses, errors = Lock(), [0], [0], [0], [], {}, []

        def steps(index):
            """模拟 index 的来源准备、媒体传输及阻塞后处理；返回完成状态。"""
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            with lock:
                active[0] -= 1
            yield "download", 0
            if index >= 2 and not processing_busy.wait(5):
                raise TimeoutError("后处理未开始")
            with lock:
                started.append(index)
                if len(started) >= 10:
                    downloaded.set()
            yield "ready", 1
            with lock:
                active[0] += 1
                processing_count[0] += 1
                peak[0] = max(peak[0], active[0])
                if processing_count[0] >= 2:
                    processing_busy.set()
            try:
                if not release.wait(10):
                    raise TimeoutError("后处理测试未释放")
            finally:
                with lock:
                    active[0] -= 1
                    processing_count[0] -= 1
            return {"status": "completed"}

        def update(item, status, value):
            """记录 item 的 status；value 为调度结果或错误，无返回值。"""
            with lock:
                statuses[item] = status

        def run():
            """运行隔离调度并收集异常；无参数或返回值。"""
            try:
                schedule([(index, steps(index)) for index in range(12)], self.config, controls, update)
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=run)
        thread.start()
        try:
            self.assertTrue(processing_busy.wait(5), "未形成持续的后处理负载")
            self.assertTrue(downloaded.wait(5), "后处理占用时后续下载未继续")
            self.assertLessEqual(peak[0], self.config["max_in_flight"])
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(sum(status == "done" for status in statuses.values()), 12)

    def test_oversized_episode_and_failure_do_not_block_following_jobs(self):
        """队列为空时允许单集超限；单项失败后其余任务仍可完成，无返回值。"""
        controls = StageControls(self.config)
        statuses = {}

        def steps(index):
            """模拟 index 下载，首集超限、第二集失败；成功时返回结果。"""
            if index == 1:
                raise ValueError("synthetic failure")
            yield "ready", 11 if index == 0 else 1
            return {"status": "completed"}

        def update(item, status, value):
            """记录 item 的 status；value 是结果或异常，无返回值。"""
            statuses[item] = status

        schedule([(index, steps(index)) for index in range(3)], self.config, controls, update)
        self.assertEqual(statuses, {0: "done", 1: "failed", 2: "done"})

    def test_retry_prepares_ahead_and_takes_next_download_slot_in_submission_order(self):
        """普通预解析已满时仍准备重试，并为尚未解析完的重试留出下一下载名额；无返回值。"""
        self.config["download"]["threads"] = 1
        controls = StageControls(self.config)
        ordinary_prepared, retry_parsing, release_source, release_download = (Event() for _ in range(4))
        download_started, download_released, dispatch_seen = Event(), Event(), Event()
        downloaded, statuses = [], {}
        sent, polls = False, 0

        def steps(item):
            """模拟 item 的来源解析和下载，使用事件控制不同任务释放名额；返回模拟结果。"""
            if item == "retry1":
                retry_parsing.set()
                if not release_source.wait(5):
                    raise TimeoutError("未释放重试来源解析")
            yield "download", 0
            downloaded.append(item)
            if item == "running":
                download_started.set()
                if not release_download.wait(5):
                    raise TimeoutError("未释放原下载")
            yield "ready", 1
            return {"status": "completed"}

        def update(item, status, value):
            """记录 item 的 status/value，并在旧预解析和下载释放时发出事件；无返回值。"""
            statuses[item] = status
            if item == "prepared" and status == "waiting_download":
                ordinary_prepared.set()
            if item == "running" and status == "waiting_queue":
                download_released.set()

        def retries(idle):
            """观察 idle 所在的调度轮次，在普通队列填满后返回两次连续重试；其余轮次返回空列表。"""
            nonlocal sent, polls
            if download_released.is_set():
                polls += 1
                if polls == 2:
                    dispatch_seen.set()
            if ordinary_prepared.is_set() and not sent:
                sent = True
                return [(item, steps(item)) for item in ("retry1", "retry2")]
            return []

        jobs = [(item, steps(item)) for item in ("running", "prepared", "later")]
        thread = Thread(target=schedule, args=(jobs, self.config, controls, update), kwargs={"retries": retries})
        thread.start()
        try:
            self.assertTrue(download_started.wait(5))
            self.assertTrue(retry_parsing.wait(5))
            self.assertEqual(downloaded, ["running"])
            release_download.set()
            self.assertTrue(dispatch_seen.wait(5))
            self.assertEqual(downloaded, ["running"])
        finally:
            release_source.set()
            release_download.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(downloaded, ["running", "retry1", "retry2", "prepared", "later"])
        self.assertEqual(len(statuses), len(downloaded))
        self.assertTrue(all(status == "done" for status in statuses.values()))

    def test_downloaded_retry_overtakes_ordinary_ready_items_without_interrupting_processing(self):
        """更早进入后处理等待队列的普通项让重试先执行，已运行的后处理不被打断；无返回值。"""
        controls = StageControls(self.config)
        normal_prepared, blocker_processing, normal_ready, retry_ready = (Event() for _ in range(4))
        release_retry, release_blocker = Event(), Event()
        processed, statuses = [], {}
        sent = False

        def steps(item):
            """模拟 item 在下载及后处理阶段等待受控事件；返回模拟完成结果。"""
            yield "download", 0
            download_gate = {"blocker": normal_prepared, "normal": blocker_processing, "retry": release_retry}[item]
            if not download_gate.wait(5):
                raise TimeoutError("未释放测试下载")
            yield "ready", 1
            processed.append(item)
            if item == "blocker":
                blocker_processing.set()
                if not release_blocker.wait(5):
                    raise TimeoutError("未释放旧后处理")
            return {"status": "completed"}

        def update(item, status, value):
            """记录 item 的 status/value 并通知预解析和等待入队阶段；无返回值。"""
            statuses[item] = status
            if item == "normal" and status == "waiting_download":
                normal_prepared.set()
            if status == "waiting_processing" and item in ("normal", "retry"):
                (normal_ready if item == "normal" else retry_ready).set()

        def retries(idle):
            """只在首次调度轮次返回一个手动重试任务；其余轮次返回空列表。"""
            nonlocal sent
            if sent:
                return []
            sent = True
            return [("retry", steps("retry"))]

        jobs = [(item, steps(item)) for item in ("blocker", "normal")]
        thread = Thread(target=schedule, args=(jobs, self.config, controls, update), kwargs={"retries": retries})
        thread.start()
        try:
            self.assertTrue(normal_ready.wait(5))
            release_retry.set()
            self.assertTrue(retry_ready.wait(5))
            self.assertEqual(processed, ["blocker"])
        finally:
            release_retry.set()
            release_blocker.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(processed, ["blocker", "retry", "normal"])
        self.assertEqual(statuses, {item: "done" for item in processed})

    def test_interrupt_closes_waiting_generators(self):
        """调度中断时设置取消信号并关闭已下载的生成器；无返回值。"""
        controls = StageControls(self.config)
        closed, statuses = Event(), {}

        def steps():
            """模拟下载后等待处理，关闭时通知测试；无最终结果。"""
            try:
                yield "ready", 1
            finally:
                closed.set()

        def update(item, status, value):
            """记录 item 的 status，在等待入队时模拟中断；value 未使用。"""
            statuses[item] = status
            if status == "waiting_queue":
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            schedule([(0, steps())], self.config, controls, update)
        self.assertTrue(controls.cancel.is_set())
        self.assertTrue(closed.is_set())
        self.assertEqual(statuses[0], "interrupted")

    def test_device_slots_prefer_gpu_and_allow_cpu(self):
        """GPU 与 CPU 各有独占槽，GPU 忙时使用 CPU；无返回值。"""
        self.config["decode_verification"] = {"gpu": True, "cpu": True}
        controls = StageControls(self.config)
        with controls.device() as first:
            with controls.device() as second:
                self.assertEqual((first, second), ("gpu", "cpu"))

    def test_same_identity_waits_until_owner_finishes(self):
        """同来源工作目录保持独占，前一任务释放后下一任务才能进入；无返回值。"""
        controls = StageControls(self.config)
        attempted, acquired = Event(), Event()

        def contender():
            """尝试获取相同来源锁，成功后发出通知；无返回值。"""
            attempted.set()
            with controls.identity("same-source"):
                acquired.set()

        with controls.identity("same-source"):
            thread = Thread(target=contender)
            thread.start()
            self.assertTrue(attempted.wait(5))
            self.assertFalse(acquired.is_set())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(acquired.is_set())


if __name__ == "__main__":
    unittest.main()
