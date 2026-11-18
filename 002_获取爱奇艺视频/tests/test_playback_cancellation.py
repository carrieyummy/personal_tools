"""隔离验证网页取消、专辑关联和调度阶段的局部停止。"""

import json
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import yaml

from qsv_local.activity_log import Journal
from qsv_local.playback.cancellation import CancellationRegistry
from qsv_local.playback.application import plan_batches
from qsv_local.playback.coordination import StageControls
from qsv_local.playback.scheduler import schedule
from qsv_local.runtime import check_cancel


class CancellationTests(unittest.TestCase):
    """使用合成链接与事件测试局部取消，不访问远程站点。"""

    def setUp(self):
        """读取现有配置并构造受控的下载、处理并发；无返回值。"""
        root = Path(__file__).resolve().parents[1]
        self.config = yaml.safe_load((root / "configs/config.yaml").read_text(encoding="utf-8-sig"))
        self.config["download"]["threads"] = self.config["max_in_flight"] = 2
        self.controls = StageControls(self.config)
        self.options = [{"index": i, "url": f"https://example.invalid/{i}"} for i in range(3)]
        self.registry = CancellationRegistry(self.options, None, self.controls.cancel, self.controls.interval)

    def test_cancel_album_including_later_links(self):
        """取消专辑后同专辑的现有和后续链接均停止，其他专辑不受影响。"""
        first = self.registry.resolved(0, {"album_id": "1", "title": "甲"})
        other = self.registry.resolved(1, {"album_id": "2", "title": "乙"})
        self.assertTrue(self.registry.cancel("album", "1"))
        later = self.registry.resolved(2, {"album_id": "1", "title": "甲"})
        self.assertTrue(first.is_set())
        self.assertTrue(later.is_set())
        self.assertFalse(other.is_set())
        self.assertFalse(self.controls.cancel.is_set())

    def test_cancel_pending_link(self):
        """待解析链接用稳定 ID 取消，其他链接保持可用。"""
        rows, _ = self.registry.snapshot()
        self.assertTrue(self.registry.cancel("link", rows[0]["id"]))
        self.assertTrue(self.registry.begin(0).is_set())
        self.assertFalse(self.registry.begin(1).is_set())

    def test_discovery_uses_snapshot_and_skips_cancelled_link(self):
        """清单变化不改变当前批次的链接顺序，取消首项仍能解析后续目标。"""
        rows, _ = self.registry.snapshot()
        self.assertTrue(self.registry.cancel("link", rows[0]["id"]))
        options = [{**row, "quality": 1, "all_parts": False} for row in self.options]
        urls = [row["url"] for row in options]
        videos = [{"index": i, "tvid": str(i + 1), "episode": 1, "album_id": str(i + 1), "title": str(i)} for i in range(3)]
        with patch("qsv_local.playback.application.linked_videos", side_effect=[[videos[1]], [videos[2]]]) as resolve, \
                patch("qsv_local.playback.application.expand_episodes", side_effect=lambda profile, link, whole, cancel=None: [link]):
            batches = list(plan_batches({}, None, cancel=self.controls.cancel, options=options, registry=self.registry))
        self.assertEqual([batch["jobs"][0]["video"]["index"] for batch in batches[1:]], [1, 2])
        self.assertFalse(batches[0]["jobs"])
        self.assertFalse(batches[0]["errors"])
        self.assertEqual(resolve.call_args_list[0].kwargs["urls"], urls)

    def test_cancel_active_download_or_processing(self):
        """下载或后处理中的单项响应取消，其他任务完成且批次信号未设置。"""
        for phase in ("download", "processing"):
            with self.subTest(phase=phase):
                started, release = Event(), Event()
                stopped = self.registry.resolved(0, {"album_id": phase, "title": phase})
                states, errors = {}, []

                def steps(index):
                    """模拟 index 的传输和后处理；目标阶段等待释放并检查局部信号。"""
                    yield "download", 0
                    if index == 0 and phase == "download":
                        started.set()
                        release.wait(5)
                        check_cancel(stopped)
                    yield "ready", 1
                    if index == 0 and phase == "processing":
                        started.set()
                        release.wait(5)
                        check_cancel(stopped)
                    return {"status": "completed"}

                def run():
                    """运行受控调度并记录异常，供主线程断言。"""
                    try:
                        schedule([(i, steps(i)) for i in range(2)], self.config, self.controls,
                                 lambda item, status, value: states.update({item: status}),
                                 is_cancelled=lambda item: item == 0 and stopped.is_set())
                    except BaseException as error:
                        errors.append(error)

                thread = Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(started.wait(5))
                    self.assertTrue(self.registry.cancel("album", phase))
                finally:
                    release.set()
                    thread.join(10)
                self.assertFalse(thread.is_alive())
                self.assertFalse(errors)
                self.assertEqual(states, {0: "cancelled", 1: "done"})
                self.assertFalse(self.controls.cancel.is_set())

    def test_page_api_validates_token_and_cancels(self):
        """本机 POST 需正确来源和令牌，成功后链接事件置位，离线页面不含令牌。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.config.update(output_path=root, work_dir=root)
            self.config["logging"]["port"] = 0
            with Journal(self.config, mode="playback") as journal:
                journal.cancellations = self.registry
                rows, _ = self.registry.snapshot()
                body = json.dumps({"kind": "link", "id": rows[0]["id"]}).encode()
                headers = {"Origin": journal.url.rstrip("/"), "Content-Type": "application/json", "X-Control-Token": "invalid"}
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(Request(journal.url + "api/cancel", data=body, headers=headers), timeout=5)
                self.assertEqual(rejected.exception.code, 403)
                self.assertFalse(self.registry.begin(0).is_set())
                headers["X-Control-Token"] = journal.control_token
                with urlopen(Request(journal.url + "api/cancel", data=body, headers=headers), timeout=5) as response:
                    self.assertEqual(response.status, 200)
                self.assertTrue(self.registry.begin(0).is_set())
                self.assertNotIn(journal.control_token, journal.render(live=False))

    def test_cancel_waiting_queue_releases_capacity(self):
        """取消等待处理的任务后释放字节容量，其他任务继续完成。"""
        self.config["max_in_flight"] = 1
        self.config["download"]["queue_capacity_gb"] = 1 / 1024**3
        controls, processing, release, queued = StageControls(self.config), Event(), Event(), Event()
        cancelled, states, errors = Event(), {}, []

        def steps(index):
            """模拟 index 下载一个字节；首项阻塞处理以积压后续队列。"""
            yield "download", 0
            if index and not processing.wait(5):
                raise TimeoutError("处理未开始")
            yield "ready", 1
            if index == 0:
                processing.set()
                if not release.wait(5):
                    raise TimeoutError("处理未释放")
            return {"status": "completed"}

        def update(index, status, value):
            """记录 index 的 status，value 为阶段结果；目标进入队列时通知测试。"""
            states[index] = status
            if index == 1 and status == "waiting_processing":
                queued.set()

        def run():
            """运行调度器并收集异常，供测试线程断言。"""
            try:
                schedule([(i, steps(i)) for i in range(3)], self.config, controls, update,
                         is_cancelled=lambda item: item == 1 and cancelled.is_set())
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=run)
        thread.start()
        try:
            self.assertTrue(queued.wait(5))
            cancelled.set()
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(states, {0: "done", 1: "cancelled", 2: "done"})
