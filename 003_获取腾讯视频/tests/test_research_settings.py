"""验证研究样本来自当前链接清单；备注和 URL 形式不影响目标选择。"""

from pathlib import Path
import json
from copy import deepcopy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import yaml

from local.configuration import load_config
from research.settings import load_research, sample_requests


class ResearchSampleTests(unittest.TestCase):
    """通过临时清单隔离检查移动、桌面及整剧链接，不访问网络或登录态。"""

    def test_empty_and_other_titles_do_not_filter_links(self):
        """空备注和任意备注都生成单集样本；排名按当前配置继承。"""
        config = load_config(Path(__file__).resolve().parents[1] / "configs/config.yaml", source="url")
        with TemporaryDirectory(prefix="research_samples_") as folder:
            listing = Path(folder) / "list.yaml"
            config["default_list"] = listing
            values = [{"url": "https://m.v.qq.com/x/m/play?cid=cover&vid=video1", "title": None},
                      {"url": "https://v.qq.com/x/cover/cover/video2.html", "title": "任意备注"},
                      {"url": "https://v.qq.com/x/page/video3.html"}]
            listing.write_text(yaml.safe_dump({"download_list": values}), encoding="utf-8")
            samples = sample_requests(config)
        self.assertEqual([item.video_id for item in samples], ["video1", "video2", "video3"])
        self.assertTrue(all(not item.all_parts for item in samples))
        self.assertTrue(all(item.quality == config["download"]["quality"] for item in samples))

    def test_cover_link_selects_catalog_first_episode(self):
        """没有 vid 的整剧链接使用公开目录首集，不依赖备注或特定剧名。"""
        config = load_config(Path(__file__).resolve().parents[1] / "configs/config.yaml", source="url")
        with TemporaryDirectory(prefix="research_cover_") as folder:
            config["default_list"] = Path(folder) / "list.yaml"
            config["default_list"].write_text(yaml.safe_dump({"all-parts": False, "download_list": [
                {"url": "https://v.qq.com/x/cover/cover.html"}]}), encoding="utf-8")
            with patch("local.playback.tasks.catalog", return_value=[{"series_id": "cover", "video_id": "first"}]) as query:
                samples = sample_requests(config)
            query.assert_called_once()
        self.assertEqual(samples[0].video_id, "first")
        self.assertFalse(samples[0].all_parts)

    def test_playback_report_accepts_mobile_and_desktop_links(self):
        """研究入口按两个不同备注链接生成页面及接口报告，桌面地址不依赖 cid/vid 查询串。"""
        from research import playback_probe
        root = Path(__file__).resolve().parents[1]
        research, config = load_research(root / "research/configs/playback_research.yaml")
        with TemporaryDirectory(prefix="research_report_") as folder:
            base = Path(folder)
            config["default_list"] = base / "list.yaml"
            config["default_list"].write_text(yaml.safe_dump({"download_list": [
                {"url": "https://m.v.qq.com/x/m/play?cid=cover&vid=video1", "title": ""},
                {"url": "https://v.qq.com/x/cover/cover/video2.html", "title": "任意剧名"}]}), encoding="utf-8")
            research["config"] = str(root / "configs/config.yaml")
            research["report_dir"] = str(base / "reports")
            path = base / "research.yaml"
            path.write_text(yaml.safe_dump(research), encoding="utf-8")
            client = Mock()
            client.profile = deepcopy(config["playback"])
            client.user = {"app_version": "synthetic"}
            client.transport.text.return_value = '<script src="https://example.invalid/player.js"></script>'
            with patch.object(playback_probe, "load_config", return_value=config), patch.object(playback_probe, "NativeClient", return_value=client), patch.object(playback_probe, "describe", return_value={}), patch("sys.argv", ["playback_probe.py", "--config", str(path)]), patch("builtins.print"):
                playback_probe.main()
            reports = list((base / "reports").glob("playback_*.json"))
            self.assertEqual(len(reports), 1)
            items = json.loads(reports[0].read_text(encoding="utf-8"))["items"]
        self.assertEqual([item["video_id"] for item in items], ["video1", "video2"])
        self.assertTrue(all(item["page_structure"]["script_hosts"] == ["example.invalid"] for item in items))


if __name__ == "__main__":
    unittest.main()
