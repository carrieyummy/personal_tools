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
        started, states, guard = [], {}, Lock()

        def steps(index):
            """模拟 index 的下载和后处理，下载产生 8 字节，返回完成结果。"""
            with guard:
                started.append(index)
            if index and not processing.wait(5):
                raise TimeoutError("首集未开始后处理")
            yield 8
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
            with guard:
                started.append(index)
                if len(started) == self.config["download"]["threads"]:
                    both_started.set()
            if not release.wait(5):
                raise TimeoutError("测试下载未释放")
            yield 1
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

    def test_oversized_episode_and_failure_do_not_block_following_jobs(self):
        """队列为空时允许单集超限；单项失败后其余任务仍可完成，无返回值。"""
        controls = StageControls(self.config)
        statuses = {}

        def steps(index):
            """模拟 index 下载，首集超限、第二集失败；成功时返回结果。"""
            if index == 1:
                raise ValueError("synthetic failure")
            yield 11 if index == 0 else 1
            return {"status": "completed"}

        def update(item, status, value):
            """记录 item 的 status；value 是结果或异常，无返回值。"""
            statuses[item] = status

        schedule([(index, steps(index)) for index in range(3)], self.config, controls, update)
        self.assertEqual(statuses, {0: "done", 1: "failed", 2: "done"})

    def test_interrupt_closes_waiting_generators(self):
        """调度中断时设置取消信号并关闭已下载的生成器；无返回值。"""
        controls = StageControls(self.config)
        closed, statuses = Event(), {}

        def steps():
            """模拟下载后等待处理，关闭时通知测试；无最终结果。"""
            try:
                yield 1
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
