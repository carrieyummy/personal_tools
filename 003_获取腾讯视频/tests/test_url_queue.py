"""用受控阶段验证 URL 下载、字节队列和后处理的并发边界。"""

from copy import deepcopy
from threading import Event, Lock, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local.configuration import load_playback_config
from local.playback.application import UrlBatch
from local.playback.transport import Transport
from local.runtime import check_cancel
from tests.test_batch import MemoryJournal
from tests.test_playback import current_config


class QueueJournal(MemoryJournal):
    """记录各任务阶段，并在指定任务等待队列时通知测试线程。"""

    def __init__(self, target: str):
        """创建日志；target 是要观察的任务视频编号。"""
        super().__init__()
        self.target, self.blocked, self.first_queued = target, Event(), Event()

    def update(self, entry, stage=None, **fields):
        """记录 entry 的 stage 和 fields；目标任务等待队列时发送通知。"""
        super().update(entry, stage, **fields)
        if entry["video_id"] == self.target and stage == "waiting_queue":
            self.blocked.set()
        if entry["video_id"] == "1" and stage == "waiting_processing":
            self.first_queued.set()


class UrlQueueTests(unittest.TestCase):
    """验证有界队列的背压和异常场景，不访问网络和真实账号。"""

    def setUp(self):
        """复制实际配置并设置测试所需的小容量与并发值。"""
        self.config = deepcopy(current_config())
        self.config["download"]["threads"] = 1
        self.config["download"]["queue_capacity_gb"] = 10 / 1024**3
        self.config["max_in_flight"] = 1
        self.config["runtime"]["cancel_poll_interval_seconds"] = 0.01

    def test_capacity_validation(self):
        """拒绝无效容量，并接受大于等于一个字节的有限正数。"""
        for value in (None, True, 0, -1, float("inf"), float("nan"), 0.1 / 1024**3):
            config = deepcopy(self.config)
            config["download"]["queue_capacity_gb"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "queue_capacity_gb"):
                load_playback_config(config, config["config_path"].parent)
        load_playback_config(self.config, self.config["config_path"].parent)

    def run_bounded_case(self, sizes: list[int]):
        """以 sizes 模拟分片大小，暂停首个后处理并检查第四集等待容量。"""
        journal = QueueJournal("3")
        transport = Transport(self.config, Event())
        batch = UrlBatch(self.config, journal, client=SimpleNamespace(transport=transport))
        release, processing_started, third_started = Event(), Event(), Event()
        lock, started, processed, result = Lock(), [], [], []

        def steps(task, entry):
            """模拟 task 的下载和后处理；entry 为日志条目，让出实际大小。"""
            self.assertEqual(len(journal.entries), len(sizes))
            if task["video_id"] == "1" and not processing_started.wait(5):
                raise TimeoutError("首集未开始后处理")
            if int(task["video_id"]) >= 2 and not journal.first_queued.wait(5):
                raise TimeoutError("第二集未进入队列")
            if task["video_id"] == "3" and not third_started.wait(5):
                raise TimeoutError("第三集未开始下载")
            with lock:
                started.append(task["video_id"])
            if task["video_id"] == "2":
                third_started.set()
            yield "ready", sizes[int(task["video_id"])]
            check_cancel(batch.cancel)
            if task["video_id"] == "0":
                processing_started.set()
                if not release.wait(10):
                    raise TimeoutError("测试后处理未释放")
            with lock:
                processed.append(task["video_id"])
            return {"status": "completed"}

        tasks = [{"video_id": str(i), "title": str(i)} for i in range(len(sizes))]
        with patch.object(batch, "_job_steps", side_effect=steps):
            thread = Thread(target=lambda: result.append(batch.run(tasks)))
            thread.start()
            try:
                self.assertTrue(processing_started.wait(5))
                self.assertTrue(journal.blocked.wait(5))
                with lock:
                    self.assertEqual(set(started), {"0", "1", "2", "3"})
                    self.assertEqual(processed, [])
                self.assertEqual(journal.entries[1]["stage"], "waiting_processing")
            finally:
                release.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["completed"], len(sizes))
        self.assertEqual(set(processed), {str(i) for i in range(len(sizes))})

    def test_waiting_queue_uses_bytes_and_backpressure(self):
        """两集不能同时放进容量时停止更多下载，处理完成后按顺序继续。"""
        # 下载并发为 2，第一集在处理，第二集入队，第三和第四集等待，故第五集尚未下载。
        self.config["download"]["threads"] = 2
        self.run_bounded_case([6, 6, 6, 6, 6])

    def test_single_oversized_episode_can_enter_empty_queue(self):
        """超大单集可独占空队列，其余剧集等待容量释放。"""
        self.config["download"]["threads"] = 2
        self.run_bounded_case([1, 12, 1, 1, 1])

    def test_retry_returns_to_download_stage(self):
        """后处理报告来源变化时重新经过下载阶段，并最终只计一次完成。"""
        journal = MemoryJournal()
        batch = UrlBatch(self.config, journal, client=SimpleNamespace(transport=Transport(self.config, Event())))
        stages = []

        def steps(task, entry):
            """以 task 和 entry 模拟一次重试，让出阶段并返回终态。"""
            stages.append("download")
            yield "ready", 1
            stages.append("process")
            yield "retry", 0
            stages.append("download")
            yield "ready", 1
            stages.append("process")
            return {"status": "completed"}

        with patch.object(batch, "_job_steps", side_effect=steps):
            result = batch.run([{"video_id": "retry", "title": "重试"}])
        self.assertEqual(stages, ["download", "process", "download", "process"])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(sum(result.values()), 1)

    def test_cancel_releases_waiting_and_processing_tasks(self):
        """取消时等待队列、活动处理和未执行任务均能结束，不遗留调度线程。"""
        self.config["download"]["threads"] = 2
        journal = QueueJournal("3")
        batch = UrlBatch(self.config, journal, client=SimpleNamespace(transport=Transport(self.config, Event())))
        processing_started, release = Event(), Event()
        result = []

        def steps(task, entry):
            """按 task 模拟下载并响应取消，entry 是对应日志项。"""
            check_cancel(batch.cancel)
            if task["video_id"] == "1":
                processing_started.wait(5)
            if int(task["video_id"]) >= 2:
                journal.first_queued.wait(5)
            yield "ready", 6
            if task["video_id"] == "0":
                processing_started.set()
                release.wait(5)
            check_cancel(batch.cancel)
            return {"status": "completed"}

        tasks = [{"video_id": str(i), "title": str(i)} for i in range(5)]
        with patch.object(batch, "_job_steps", side_effect=steps):
            thread = Thread(target=lambda: result.append(batch.run(tasks)))
            thread.start()
            try:
                self.assertTrue(journal.blocked.wait(5))
                batch.cancel.set()
            finally:
                release.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["interrupted"], len(tasks))

    def test_keyboard_interrupt_drains_suspended_jobs(self):
        """用户中断时回收已挂起任务并标记未派发任务，不丢失最终计数。"""
        journal = MemoryJournal()
        batch = UrlBatch(self.config, journal, client=SimpleNamespace(transport=Transport(self.config, Event())))
        finalized = Event()

        def steps(task, entry):
            """模拟 task 下载后的挂起点；entry 为日志，退出时通知测试。"""
            try:
                check_cancel(batch.cancel)
                yield "ready", 6
                check_cancel(batch.cancel)
                return {"status": "completed"}
            finally:
                finalized.set()

        tasks = [{"video_id": str(i), "title": str(i)} for i in range(2)]
        with patch.object(batch, "_job_steps", side_effect=steps), \
             patch("local.playback.application.wait", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                batch.run(tasks)
        self.assertTrue(finalized.is_set())
        self.assertEqual(batch.summary["interrupted"], len(tasks))


if __name__ == "__main__":
    unittest.main()
