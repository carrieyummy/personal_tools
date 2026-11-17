"""以同步事件验证下载池和共享处理池的独立限流、续签及取消；不访问真实会话。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock, current_thread
import unittest

from local.configuration import load_config
from local.runtime import TaskCancelled, check_cancel
from local.scheduler import Scheduler, PendingDownload
from tests.test_pipeline import JournalStub


class SchedulerStub(Scheduler):
    """使用可注入阶段生成器执行生产调度，收集内存日志。"""

    def __init__(self, config, job):
        """config 为已校验配置，job(task, entry) 返回阶段生成器；初始化日志及取消信号。"""
        self.config, self.job = config, job
        self.cancel, self.journal = Event(), JournalStub()
        self.interval = config["runtime"]["cancel_poll_interval_seconds"]

    def dispatch_stop_reason(self):
        """测试不模拟全局认证停止；返回 None。"""
        return None

    def notify(self, entry, stage, **fields):
        """将 entry 的 stage 和 fields 写入日志；无返回值。"""
        self.journal.update(entry, stage, **fields)

    def _job_steps(self, task, entry):
        """将 task 和 entry 传给测试生成器，返回最终报告。"""
        return (yield from self.job(task, entry))


class SchedulerTests(unittest.TestCase):
    """根据当前 YAML 的并发值构造事件屏障，检查实际占用而非固定默认值。"""

    def setUp(self):
        """读取用户配置并初始化分阶段计数；无参数及返回值。"""
        self.config = load_config(Path(__file__).resolve().parents[1] / "configs/config.yaml")
        self.lock = Lock()
        self.active = {"download": 0, "processing": 0}
        self.peaks = dict(self.active)

    @contextmanager
    def occupied(self, kind):
        """统计 kind 对应阶段的当前及峰值并发；退出时释放，无返回值。"""
        with self.lock:
            self.active[kind] += 1
            self.peaks[kind] = max(self.peaks[kind], self.active[kind])
        try:
            expected = "youku-download" if kind == "download" else "youku-process"
            self.assertTrue(current_thread().name.startswith(expected))
            yield
        finally:
            with self.lock:
                self.active[kind] -= 1

    @staticmethod
    def tasks(count):
        """生成 count 个具有稳定编号的测试任务，返回任务列表。"""
        return [{"video_id": str(index), "index": index} for index in range(count)]

    def test_full_download_pool_overlaps_with_resolution(self):
        """全部下载名额占用时，其他任务仍能在共享处理池解析，且两个上限各自独立。"""
        downloads = self.config["download"]["threads"]
        processing = self.config["max_in_flight"]
        overlap = min(downloads, processing)
        full, resolving, release = Event(), Event(), Event()

        def job(task, entry):
            """解析 task 后下载并后处理；entry 为日志，事件阻塞第二批解析以观察并发。"""
            with self.occupied("processing"):
                if task["index"] >= downloads:
                    if not full.wait(timeout=10):
                        raise TimeoutError("downloads did not fill")
                    with self.lock:
                        if self.active["processing"] == overlap:
                            resolving.set()
                    if not release.wait(timeout=10):
                        raise TimeoutError("resolution gate")
            yield "download", 0
            with self.occupied("download"):
                with self.lock:
                    if self.active["download"] == downloads:
                        full.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("download gate")
            yield "ready", 1
            with self.occupied("processing"):
                return {"status": "completed"}

        batch = SchedulerStub(self.config, job)
        tasks = self.tasks(downloads + overlap)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(batch.run, tasks)
            try:
                self.assertTrue(full.wait(timeout=5))
                self.assertTrue(resolving.wait(timeout=5))
                with self.lock:
                    self.assertEqual(self.active["download"], downloads)
                    self.assertEqual(self.active["processing"], overlap)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=15)["completed"], len(tasks))
        self.assertEqual(self.peaks["download"], downloads)
        self.assertLessEqual(self.peaks["processing"], processing)

    def test_resolution_postprocessing_and_refresh_share_limit(self):
        """初始解析、后处理、续签及来源重试全部在同一处理池运行，不占下载线程。"""
        processing = self.config["max_in_flight"]

        def job(task, entry):
            """为 task 模拟两次下载、续签及来源变化重试；entry 为日志，最后返回成功。"""
            for attempt in range(2):
                with self.occupied("processing"):
                    pass
                yield "download", 0
                with self.occupied("download"):
                    pass
                yield "resolve", 0
                with self.occupied("processing"):
                    pass
                yield "download", 0
                with self.occupied("download"):
                    pass
                yield "ready", 1
                with self.occupied("processing"):
                    pass
                if attempt == 0:
                    yield "retry", 0
            return {"status": "completed"}

        batch = SchedulerStub(self.config, job)
        tasks = self.tasks(self.config["download"]["threads"] + processing)
        self.assertEqual(batch.run(tasks)["completed"], len(tasks))
        self.assertLessEqual(self.peaks["download"], self.config["download"]["threads"])
        self.assertLessEqual(self.peaks["processing"], processing)

    def test_reused_and_resumed_tasks_never_enter_download_pool(self):
        """直接复用及候选续发跳过下载池；单项失败、认证跳过不阻塞后续任务。"""
        def job(task, entry):
            """按 task 编号模拟复用、候选续发或异常，entry 为日志；返回对应终态。"""
            with self.occupied("processing"):
                if task["index"] == 0:
                    return {"status": "reused"}
                if task["index"] == 1:
                    raise ValueError("fixture failure")
                if task["index"] == 2:
                    raise PendingDownload("fixture authentication")
            yield "ready", 1
            with self.occupied("processing"):
                return {"status": "completed"}

        batch = SchedulerStub(self.config, job)
        summary = batch.run(self.tasks(4))
        self.assertEqual({key: summary[key] for key in ("completed", "reused", "failed", "skipped")},
                         {"completed": 1, "reused": 1, "failed": 1, "skipped": 1})
        self.assertEqual(self.peaks["download"], 0)

    def test_cancel_closes_prepared_tasks_without_downloading(self):
        """下载满载且已有预解析任务等待时，取消关闭所有生成器并完整登记终态。"""
        downloads = self.config["download"]["threads"]
        prepared, full = Event(), Event()
        closed = set()

        def job(task, entry):
            """task 在下载槽等待取消；entry 为日志，finally 记录生成器释放。"""
            try:
                if task["index"] >= downloads:
                    prepared.set()
                yield "download", 0
                with self.occupied("download"):
                    with self.lock:
                        if self.active["download"] == downloads:
                            full.set()
                    batch.cancel.wait(timeout=10)
                    check_cancel(batch.cancel)
                yield "ready", 1
                return {"status": "completed"}
            finally:
                with self.lock:
                    closed.add(task["index"])

        batch = SchedulerStub(self.config, job)
        tasks = self.tasks(downloads * 3)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(batch.run, tasks)
            try:
                self.assertTrue(full.wait(timeout=5))
                self.assertTrue(prepared.wait(timeout=5))
            finally:
                batch.cancel.set()
            with self.assertRaises(TaskCancelled):
                future.result(timeout=15)
        self.assertEqual(batch.summary["interrupted"], len(tasks))
        self.assertEqual(self.active, {"download": 0, "processing": 0})
        self.assertGreater(len(closed), downloads)
        self.assertTrue(all(entry["status"] == "interrupted" for entry in batch.journal.entries))


if __name__ == "__main__":
    unittest.main()
