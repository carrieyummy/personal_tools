"""隔离验证目录增量交接、共享并发、取消、认证停止及批量日志；不访问客户端或网络。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from pathlib import Path
import json
import tempfile
from threading import Event, Lock, RLock, current_thread
import unittest
from unittest.mock import Mock, patch

from local.configuration import load_config
from local.application import run_url_batch
from local.journal import Journal
from local.runtime import TaskCancelled, check_cancel
from local.tasks import Discovery, discover
from local.youku.client import AuthenticationError
from tests.test_scheduler import SchedulerStub
from tests.test_pipeline import JournalStub


class CatalogClient:
    """以当前配置和内存目录模拟官方节目接口；钩子只用于事件同步及注入失败。"""

    def __init__(self, config: dict):
        """使用 config 的播放专项配置创建三部测试剧及取消信号；不加载真实会话。"""
        self.profile, self.cancel, self.stopped = config["playback"], Event(), Event()
        self.context = {"utid":"fixture"}
        count = max(2, config["download"]["threads"])
        self.shows = {name:[{"vid":f"{name}{number}", "title":f"{name}第{number}集", "stage":str(number),
                            "videoType":"正片", "seconds":60} for number in range(1, count + 1)]
                      for name in ("A", "B", "C")}
        self.ups_calls, self.catalog_calls = [], []
        self.ups_hook, self.catalog_hook = None, None

    def ups(self, vid: str) -> tuple[dict, str]:
        """按 vid 返回所属节目；记录调用并执行可选 ups_hook，挑战仅为测试占位。"""
        check_cancel(self.cancel)
        self.ups_calls.append(vid)
        if self.ups_hook:
            self.ups_hook(vid)
        return {"show":{"encodeid":vid[0], "title":vid[0]}}, "fixture"

    def catalog(self, vid: str, sid: str, page: int) -> dict:
        """返回 vid/sid 对应目录的第 page 页；分页数取配置允许范围内最多两页。"""
        self.catalog_calls.append((sid, page))
        if self.catalog_hook:
            self.catalog_hook(vid, sid, page)
        pages = min(2, self.profile["catalog"]["max_pages"])
        episodes = self.shows[sid]
        values = episodes[:1] if pages == 2 and page == 1 else episodes[1:] if pages == 2 else episodes
        return {"showInfoDTO":{"showId":sid, "showName":sid}, "curPage":page,
                "totalPages":pages, "total":len(episodes), "videoItemList":deepcopy(values)}


class DiscoveryTests(unittest.TestCase):
    """从当前配置派生任务量，用事件确认先下载后续目录尚未完成，检查所有终态。"""

    def setUp(self):
        """加载用户配置及隔离客户端，初始化跨目录、逐集解析、后处理共用的并发计数。"""
        self.config = load_config(Path(__file__).resolve().parents[1] / "configs/config.yaml")
        self.client = CatalogClient(self.config)
        self.lock, self.processing, self.peak = Lock(), 0, 0
        release = patch("local.tasks.release_date", return_value="2000-01-01")
        release.start()
        self.addCleanup(release.stop)

    def requests(self, names=("A", "B", "C"), whole=True):
        """按 names 顺序构造清单请求；whole 控制整剧或单集，画质读取当前配置。"""
        return [{"video_id":name+"1", "note":name, "url":"https://v.youku.com/video?vid="+name+"1",
                 "list_index":index, "quality":self.config["download"]["quality"], "all_parts":whole}
                for index, name in enumerate(names)]

    @contextmanager
    def occupied(self):
        """统计测试非下载阶段的并发及线程归属；退出释放计数，无参数及返回值。"""
        self.assertTrue(current_thread().name.startswith("youku-process"))
        with self.lock:
            self.processing += 1
            self.peak = max(self.peak, self.processing)
        try:
            yield
        finally:
            with self.lock:
                self.processing -= 1

    def source(self, items):
        """以 items 创建生产 Discovery，并为其 advance 添加共享处理池占用统计；返回来源对象。"""
        source = Discovery(self.client, items)
        advance = source.advance

        def counted():
            """在处理池计数范围内展开一项生产目录；返回完整交接事件。"""
            with self.occupied():
                return advance()

        source.advance = counted
        return source

    def test_download_overlaps_slow_catalog_and_scheduler_keeps_advancing(self):
        """后续目录阻塞时首部剧仍在下载；下载完成事件仍被主循环接收，目录共享处理上限。"""
        download_started, catalog_started, release_catalog, release_media, received = (Event() for _ in range(5))

        def catalog_hook(vid, sid, page):
            """阻塞 B 的首页直到测试放行；vid/sid/page 是生产目录请求参数。"""
            if sid == "B" and page == 1:
                if not download_started.wait(timeout=10):
                    raise TimeoutError("first download did not start")
                catalog_started.set()
                if not release_catalog.wait(timeout=10):
                    raise TimeoutError("catalog gate")

        def job(task, entry):
            """为 task 模拟处理、下载及后处理；entry 接收状态，下载由测试事件放行。"""
            with self.occupied():
                pass
            yield "download", 0
            self.assertTrue(current_thread().name.startswith("youku-download"))
            batch.notify(entry, "downloading", status="running")
            download_started.set()
            if not release_media.wait(timeout=10):
                raise TimeoutError("media gate")
            yield "ready", 1
            with self.occupied():
                return {"status":"completed"}

        self.client.catalog_hook = catalog_hook
        source = self.source(self.requests(("A", "B")))
        batch = SchedulerStub(self.config, job)
        self.client.cancel = batch.cancel
        update = batch.journal.update

        def observed(entry, stage=None, **values):
            """转发 entry 的 stage/values；下载完成被调度器消费时发信号。"""
            update(entry, stage, **values)
            if stage == "waiting_queue":
                received.set()

        with patch.object(batch.journal, "update", side_effect=observed), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(batch.run, [], source)
            try:
                self.assertTrue(download_started.wait(timeout=5))
                self.assertTrue(catalog_started.wait(timeout=5))
                self.assertEqual(batch.journal.run["discovery"]["processed_requests"], 1)
                release_media.set()
                self.assertTrue(received.wait(timeout=5), "目录请求不应阻塞调度主循环")
                self.assertFalse(release_catalog.is_set())
            finally:
                release_catalog.set()
                release_media.set()
            summary = future.result(timeout=20)
        self.assertEqual(summary["completed"], sum(len(self.client.shows[name]) for name in ("A", "B")))
        self.assertEqual(summary["total"], summary["completed"])
        self.assertEqual(batch.journal.run["discovery"]["status"], "completed")
        self.assertLessEqual(self.peak, self.config["max_in_flight"])
        self.assertEqual(self.processing, 0)

    def test_completed_task_receives_later_duplicate_membership_once(self):
        """任务已经复用完成后，后续重复链接只补充分组；不同画质仍生成独立任务。"""
        completed, calls = Event(), []

        def job(task, entry):
            """记录 task 身份并直接复用，entry 为日志；保留生成器协议但不进入下载池。"""
            with self.occupied():
                calls.append((task["video_id"], task["quality"]))
                completed.set()
                return {"status":"reused"}
            yield "download", 0

        items = self.requests(("A", "A", "A"), whole=False)
        items[-1]["quality"] += 1

        def ups_hook(vid):
            """第二项请求 vid 时等待第一项完成，验证迟到分组可更新终态条目。"""
            if len(self.client.ups_calls) == 2 and not completed.wait(timeout=10):
                raise TimeoutError("first task did not finish")

        self.client.ups_hook = ups_hook
        batch = SchedulerStub(self.config, job)
        self.client.cancel = batch.cancel
        summary = batch.run([], self.source(items))
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["reused"], 2)
        self.assertEqual(len(calls), len(set(calls)))
        first = batch.journal.entries[0]
        self.assertEqual(first["status"], "reused")
        self.assertEqual([group["id"] for group in first["groups"]], ["0", "1"])
        self.assertEqual(len(self.client.catalog_calls), min(2, self.client.profile["catalog"]["max_pages"]))

    def test_single_processing_and_download_slot_allow_catalog_overlap(self):
        """下载、处理均为一个名额时仍能重叠；慢目录只占处理名额，不阻塞下载及主循环。"""
        self.config = deepcopy(self.config)
        self.config["download"]["threads"] = 1
        self.config["max_in_flight"] = 1
        self.test_download_overlaps_slow_catalog_and_scheduler_keeps_advancing()

    def test_bad_episode_does_not_partially_dispatch_and_next_show_continues(self):
        """一部剧末集元数据无效时整项失败，不提前交接前几集；其他清单项继续处理。"""
        self.client.shows["B"][-1]["seconds"] = 0

        def job(task, entry):
            """直接复用合法 task，entry 为日志；不运行真实传输或后处理。"""
            return {"status":"reused"}
            yield "download", 0

        batch = SchedulerStub(self.config, job)
        self.client.cancel = batch.cancel
        summary = batch.run([], self.source(self.requests()))
        count = len(self.client.shows["A"]) + len(self.client.shows["C"])
        self.assertEqual(summary["reused"], count)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["total"], count + 1)
        self.assertFalse(any(entry.get("series_id") == "B" for entry in batch.journal.entries))
        self.assertEqual(batch.journal.run["discovery"]["status"], "completed")

    def test_duplicate_page_is_rejected_before_handoff(self):
        """官方目录同页出现重复视频时，整剧校验失败且不交接任何任务。"""
        self.client.shows["A"][-1] = deepcopy(self.client.shows["A"][0])
        result = Discovery(self.client, self.requests(("A",))).advance()
        self.assertEqual(result["tasks"], [])
        self.assertEqual(len(result["failures"]), 1)
        self.assertIn("重复", result["failures"][0]["detail"])

    def test_sync_discovery_preserves_order_and_all_groups(self):
        """验收使用的同步接口沿用同一目录逻辑，保持集序、画质去重及全部归属。"""
        tasks, failed = discover(self.client, self.requests(("A", "A", "C")))
        self.assertEqual(failed, [])
        self.assertEqual([task["video_id"] for task in tasks],
                         [item["vid"] for name in ("A", "C") for item in self.client.shows[name]])
        self.assertTrue(all(len(task["groups"]) == 2 for task in tasks if task["series_id"] == "A"))

    def test_cancel_during_discovery_stops_new_requests_and_records_unknown_items(self):
        """慢目录收到取消后不请求后续链接；已发现任务及未展开清单均有终态、数量不虚构。"""
        entered = Event()

        def catalog_hook(vid, sid, page):
            """B 的目录请求等待共享取消信号；vid/page 为模拟生产请求参数。"""
            if sid == "B":
                entered.set()
                self.client.cancel.wait(timeout=10)
                check_cancel(self.client.cancel)

        def job(task, entry):
            """task 下载阶段等待取消，entry 为日志；取消后不继续后处理。"""
            yield "download", 0
            batch.cancel.wait(timeout=10)
            check_cancel(batch.cancel)
            yield "ready", 1

        self.client.catalog_hook = catalog_hook
        source = self.source(self.requests())
        batch = SchedulerStub(self.config, job)
        self.client.cancel = batch.cancel
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(batch.run, [], source)
            try:
                self.assertTrue(entered.wait(timeout=5))
            finally:
                batch.cancel.set()
            with self.assertRaises(TaskCancelled):
                future.result(timeout=15)
        self.assertEqual(self.client.ups_calls, ["A1", "B1"])
        self.assertEqual(batch.summary["interrupted"], len(self.client.shows["A"]) + 2)
        self.assertEqual(batch.summary["total"], batch.summary["interrupted"])
        self.assertTrue(all(entry["status"] == "interrupted" for entry in batch.journal.entries))
        progress = batch.journal.run["discovery"]
        self.assertEqual((progress["status"], progress["processed_requests"], progress["remaining_requests"]),
                         ("interrupted", 1, 2))
        self.assertEqual(self.processing, 0)

    def test_authentication_stop_does_not_expand_remaining_links(self):
        """首项认证失败停止发现；剩余链接按清单项记为跳过，不再调用网络替身。"""
        def rejected(vid):
            """模拟请求 vid 触发共享认证停止，并抛出脱敏认证异常。"""
            self.client.stopped.set()
            raise AuthenticationError("fixture authentication exhausted")

        def job(task, entry):
            """任何 task 派发都视为错误；entry 为日志，本场景不应执行本生成器。"""
            raise AssertionError("no media task should be dispatched")
            yield "download", 0

        self.client.ups_hook = rejected
        batch = SchedulerStub(self.config, job)
        batch.dispatch_stop_reason = lambda: "认证恢复失败" if self.client.stopped.is_set() else None
        self.client.cancel = batch.cancel
        summary = batch.run([], self.source(self.requests()))
        self.assertEqual(self.client.ups_calls, ["A1"])
        self.assertEqual((summary["failed"], summary["skipped"], summary["total"]), (1, 2, 3))
        self.assertEqual(batch.journal.run["discovery"]["status"], "stopped")

    def test_journal_registers_whole_show_and_memberships_in_one_save(self):
        """整剧条目、发现进度及已完成条目的迟到分组只保存一次；快照包含独立的进度副本。"""
        journal = Journal.__new__(Journal)
        journal.lock, journal.closed = RLock(), False
        journal.run = {"id":"fixture", "entries":[]}
        journal.history = {"runs":[journal.run]}
        source = Discovery(self.client, self.requests(("A",)))
        event = source.advance()
        with patch.object(journal, "save") as save:
            entries = journal.add_many(event["tasks"], event["progress"])
            self.assertEqual(save.call_count, 1)
            self.assertEqual(len(entries), len(self.client.shows["A"]))
            save.reset_mock()
            entries[0]["status"] = "completed"
            group = {"id":"later", "title":"迟到分组", "url":"fixture"}
            journal.add_many([], event["progress"], [(entries[0], group)])
            self.assertEqual(save.call_count, 1)
        group["title"] = "changed"
        snapshot = journal.snapshot()
        self.assertEqual(entries[0]["status"], "completed")
        self.assertEqual(entries[0]["groups"][-1]["title"], "迟到分组")
        self.assertEqual(snapshot["runs"][0]["discovery"]["status"], "completed")
        event["progress"]["status"] = "changed"
        self.assertEqual(journal.run["discovery"]["status"], "completed")

    def test_reopening_journal_marks_unfinished_catalog_interrupted(self):
        """重开日志将旧批次及尚未完成的目录标为中断，保留已展开数和真实未知范围。"""
        config = deepcopy(self.config)
        progress = Discovery(self.client, self.requests()).progress()
        progress["processed_requests"] = 1
        with tempfile.TemporaryDirectory() as temporary:
            config["output_path"] = Path(temporary)
            path = config["output_path"] / config["logging"]["state_file"]
            path.write_text(json.dumps({"version":1, "runs":[{
                "id":"old", "status":"running", "entries":[], "discovery":progress}]}), encoding="utf-8")
            with patch("local.journal.ThreadingHTTPServer"), patch("local.journal.Thread"):
                journal = Journal(config)
            previous = journal.history["runs"][0]
            self.assertEqual(previous["status"], "interrupted")
            self.assertEqual(previous["discovery"]["status"], "interrupted")
            self.assertEqual(previous["discovery"]["remaining_requests"], len(self.requests()) - 1)

    def test_application_entry_uses_incremental_discovery_and_returns_all_counts(self):
        """正式批次入口通过生产调度器执行增量来源，返回失败和成功计数并关闭日志。"""
        self.client.shows["B"][-1]["seconds"] = 0

        def job(task, entry):
            """合法 task 直接复用；entry 为内存日志，不执行下载或原生处理。"""
            return {"status":"reused"}
            yield "download", 0

        batch = SchedulerStub(self.config, job)
        batch.client = self.client
        self.client.cancel = batch.cancel
        journal = JournalStub()
        journal.url, journal.close = "fixture", Mock(return_value=Path("offline.html"))
        batch.journal = journal
        with patch("local.application.output_lock", return_value=nullcontext()), \
                patch("local.application.Journal", return_value=journal), \
                patch("local.application.UrlBatch", return_value=batch), \
                patch("local.application.requests", return_value=self.requests()):
            summary = run_url_batch(self.config)
        count = len(self.client.shows["A"]) + len(self.client.shows["C"])
        self.assertEqual((summary["total"], summary["reused"], summary["failed"]), (count + 1, count, 1))
        journal.close.assert_called_once_with("warning", summary)
        self.assertEqual(journal.run["discovery"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
