"""验证专辑分组、单集页面链接与历史日志，不读取真实账号或网络。"""

from threading import Event
from unittest.mock import Mock

from local.activity import Activity, page
from local.application import Pipeline
from local.client import episode_url, normalize_url
from local.tasks import Episode
from tests.test_workflow import BaseCase
import yaml


class ActivityPageTests(BaseCase):
    """用临时日志验证节目元数据从阶段到网页及离线报告的传递。"""

    def test_series_group_and_episode_links_persist(self):
        """两集共用专辑分组，各自链接跨阶段、重启及离线页面保留；无返回值。"""
        from playwright.sync_api import sync_playwright

        link = yaml.safe_load(self.config["default_list"].read_text(encoding="utf-8-sig"))["download_list"][0]
        _, clip_id, video_id = normalize_url(link)
        self.config["output_path"].mkdir()
        activity = Activity(self.config)
        pipeline = Pipeline(self.config, Mock(), activity, Mock(), self.cancel, Event())
        videos = [Episode(clip_id, video_id, 1, "第一集", "官方专辑", True, self.config["download"]["quality"]),
                  Episode(clip_id, str(int(video_id) + 1), 2, "第二集", "官方专辑", True, self.config["download"]["quality"])]
        for video in videos:
            pipeline.stage(video, "等待")
            pipeline.stage(video, "下载", "1/2")
        state = activity.snapshot()
        items = state["runs"][-1]["items"]
        for video in videos:
            self.assertEqual(items[video.key]["series"], video.series)
            self.assertEqual(items[video.key]["page_url"], episode_url(video.clip_id, video.video_id))
        report = activity.finish("已完成")
        self.assertEqual(Activity(self.config).snapshot()["runs"][-2]["items"][videos[0].key]["page_url"],
                         episode_url(clip_id, video_id))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server"])
            try:
                tab = browser.new_page()
                tab.goto(report.as_uri())
                self.assertEqual(tab.locator("details.group > summary").all_text_contents(), ["官方专辑"])
                tab.locator("details.group details > summary").first.click()
                self.assertEqual(tab.locator("details.group details a").first.get_attribute("href"), episode_url(clip_id, video_id))
                tab.locator("details.group details > summary").last.click()
                self.assertEqual(tab.locator("details.group details a").last.get_attribute("href"), episode_url(clip_id, videos[1].video_id))
            finally:
                browser.close()

    def test_historical_records_without_links_render(self):
        """旧记录无需补写链接也能生成独立报告；无返回值。"""
        state = {"runs": [{"id": "old", "started": "2000", "status": "完成", "items": {
            "old": {"title": "旧记录", "stage": "成功"}}}]}
        html = page(state, self.config["logging"]["refresh_interval_seconds"], False)
        self.assertIn("旧记录", html)
