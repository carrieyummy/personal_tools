"""使用回环服务和隔离历史验证腾讯视频网页及离线报告。"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

import yaml

from local.journal import Journal
from local.playback.errors import PlaybackError
from local.playback.tasks import Request, discover


class JournalPageTests(unittest.TestCase):
    """以实际配置为基础，在临时输出目录中验证页面行为。"""

    def setUp(self):
        """建立隔离输出目录及随机端口，无参数和返回值。"""
        self.temp = tempfile.TemporaryDirectory(prefix="tencent_page_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = deepcopy(yaml.safe_load(Path("configs/config.yaml").read_text(encoding="utf-8")))
        self.config["output_path"] = self.root
        self.config["logging"].update(port=0, refresh_interval_seconds=0.1, final_state_timeout_seconds=0.6)
        self.journal = Journal(self.config)
        self.addCleanup(self.stop_journal)

    def stop_journal(self):
        """测试异常时回收尚未关闭的日志服务，无参数和返回值。"""
        if self.journal.thread.is_alive():
            self.journal.close("interrupted", {})

    def get(self, path):
        """读取回环服务 path 并返回字节；HTTP 错误交给调用方处理。"""
        with urlopen(self.journal.url + path.lstrip("/"), timeout=3) as response:
            return response.read()

    def test_history_snapshot_and_safe_media(self):
        """验证白名单快照、已登记成品限制、特殊字符和历史报告，无返回值。"""
        media = self.root / "video.mp4"
        media.write_bytes(b"media")
        entry = self.journal.add({"video_id": "vid1", "title": "<script>标题</script>"})
        self.journal.update(entry, status="completed", output=str(media), source={"secret": "ticket-value"})
        state = json.loads(self.get("state"))
        self.assertNotIn("ticket-value", json.dumps(state))
        self.assertEqual(self.get(state["runs"][-1]["entries"][0]["file_url"]), b"media")
        outside = self.root.parent / "unregistered.mp4"
        self.journal.update(entry, output=str(outside))
        with self.assertRaises(HTTPError) as raised:
            self.get("files/0/0")
        self.assertEqual(raised.exception.code, 404)
        html = self.get("snapshot").decode("utf-8")
        self.assertIn("\\u003cscript\\u003e", html)
        self.assertNotIn("ticket-value", html)
        report = self.journal.close("completed", {})
        self.assertIn("腾讯视频任务进度", report.read_text(encoding="utf-8"))
        restarted = Journal(self.config)
        try:
            self.assertEqual(len(restarted.snapshot()["runs"]), 2)
            self.assertEqual(restarted.snapshot()["runs"][0]["entries"][0]["video_id"], "vid1")
        finally:
            restarted.close("completed", {})

    def test_unresponsive_client_times_out(self):
        """已打开但不确认的页面在配置期限后释放服务，无参数和返回值。"""
        self.get("")
        started = time.monotonic()
        self.journal.close("completed", {})
        self.assertGreaterEqual(time.monotonic() - started, self.config["logging"]["final_state_timeout_seconds"])

    def test_duplicate_url_memberships_and_failed_group(self):
        """同一集跨 URL 保留分组且只执行一次，失败 URL 保留独立分组，无返回值。"""
        quality = self.config["download"]["quality"]
        task = {"series_id": "cover", "video_id": "vid", "title": "第一集", "series": "剧名", "quality": quality}
        values = [Request("cover", "vid", quality, True, "整剧"),
                  Request("cover", "vid", quality, False, "单集"),
                  Request("bad", "", quality, True, "失败组")]
        with patch("local.playback.tasks.catalog", side_effect=[[dict(task)], [dict(task)], PlaybackError("选集失败")]):
            tasks, failed = discover(None, values)
        self.assertEqual(len(tasks), 1)
        self.assertEqual([group["title"] for group in tasks[0]["groups"]], ["整剧", "单集"])
        self.assertEqual(failed[0]["groups"][0]["title"], "失败组")
        self.journal.add(tasks[0])
        self.assertEqual(self.journal.snapshot()["runs"][-1]["entries"][0]["groups"], tasks[0]["groups"])

    def test_browser_live_filter_export_and_final_ack(self):
        """浏览器验证更新、筛选、详情、离线快照和最终状态确认，无参数和返回值。"""
        from playwright.sync_api import sync_playwright

        entry = self.journal.add({"video_id": "url_video", "title": "URL 第一集",
                                  "groups": [{"id": "first", "title": "第一组", "url": "https://v.qq.com/x/cover/cover1.html"}]})
        other = self.journal.add({"video_id": "cache_video", "title": "缓存第二集",
                                  "groups": [{"id": "second", "title": "第二组", "url": "https://v.qq.com/x/cover/cover2.html"}]})
        self.journal.update(other, status="failed", detail="还原失败")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server"])
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(self.journal.url)
                self.assertEqual(page.locator(".task-row").count(), 2)
                self.assertEqual(page.locator(".entry-row").count(), 2)
                self.assertEqual(page.locator(".detail-row").count(), 0)
                page.locator(".task-row button").first.click()
                self.assertEqual(page.locator(".entry-row").count(), 1)
                page.locator(".task-row button").first.click()
                page.locator("#search").fill("缓存")
                self.assertEqual(page.locator(".entry-row").count(), 1)
                page.locator("#errors").check()
                self.assertIn("还原失败", page.locator(".entry-row").inner_text())
                page.locator("#search").fill("URL")
                self.assertEqual(page.locator(".entry-row").count(), 0)
                page.locator("#errors").uncheck()
                self.journal.update(entry, "downloading", status="running", progress=42.0, device="gpu")
                page.wait_for_function("document.querySelector('.entry-row')?.textContent.includes('42.0%')")
                page.locator(".entry-row button").click()
                self.assertIn("url_video", page.locator(".detail-panel").inner_text())
                self.assertEqual(page.locator(".device-cell").inner_text(), "GPU")
                self.assertTrue(page.locator(".entry-row").evaluate("row => row.nextElementSibling.classList.contains('detail-row')"))
                page.locator("#search").fill("")
                self.assertTrue(page.locator(".entry-row").first.evaluate("row => row.nextElementSibling.classList.contains('detail-row') && row.nextElementSibling.nextElementSibling.classList.contains('task-row')"))
                page.locator("#search").fill("URL")
                offline_path = self.root / "snapshot.html"
                offline_path.write_bytes(self.get("snapshot"))
                offline = browser.new_page()
                offline.goto(offline_path.as_uri())
                self.assertIn("离线快照", offline.locator("#connection").inner_text())
                self.journal.update(entry, status="completed", progress=100.0)
                page.wait_for_function("document.querySelector('.entry-row')?.textContent.includes('100.0%')")
                self.assertIn("42.0%", offline.locator(".entry-row").first.inner_text())
                thread = Thread(target=self.journal.close, args=("completed", {}))
                thread.start()
                page.wait_for_function("document.querySelector('#connection').textContent.includes('运行已结束')")
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 1280)
            finally:
                browser.close()
