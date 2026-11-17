"""验证链接下载网页按单集记录阶段并与旧 QSV 历史隔离。"""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import yaml

from qsv_local.activity_log import Journal


class PlaybackJournalTests(unittest.TestCase):
    """使用实际日志配置副本和临时路径，验证链接记录与阶段转换。"""

    def setUp(self):
        """加载当前主配置并建立临时成品、现场目录；无返回值。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = yaml.safe_load((Path(__file__).resolve().parents[1] / "configs/config.yaml").read_text(encoding="utf-8"))
        self.config.update(output_path=root / "output", work_dir=root / "work")
        self.config["output_path"].mkdir()
        self.config["work_dir"].mkdir()
        self.config["logging"]["port"] = 0
        self.video = {"album_id": "123", "title": "测试剧", "episode": 1, "episode_title": "测试剧第1集", "tvid": "456"}

    def test_episode_download_and_processing_are_independent(self):
        """下载结束后解析进度独立计量，另一集失败不改变本集；无返回值。"""
        with Journal(self.config, mode="playback") as journal:
            first = journal.add_episode(self.video, 1, self.config["audio"]["quality"])
            other = journal.add_episode(dict(self.video, episode=2, tvid="457"), 1, self.config["audio"]["quality"])
            journal.update(first, "downloading", progress=50, current_bytes=50, total_bytes=100)
            journal.update(first, "decrypting", progress=20)
            journal.update(other, "resolving_source", status="failed", error="合成解析失败")
            data = journal.snapshot(live=False)["runs"][-1]["entries"]
            self.assertEqual(data[0]["download"]["progress"], 100)
            self.assertEqual(data[0]["processing"]["progress"], 20)
            self.assertEqual(data[1]["status"], "failed")
            self.assertEqual(data[0]["directory"], self.video["album_id"])
            page = journal.render(live=False)
            self.assertIn("下载进度", page)
            self.assertIn("解析与保存进度", page)
            self.assertNotIn("__DATA__", page)
            self.assertFalse((self.config["output_path"] / self.config["logging"]["state_file"]).exists())

    def test_verification_device_is_retained_in_live_and_offline_state(self):
        """实际验证设备在阶段切换及完成后保留，未验证的历史条目不显示设备；无返回值。"""
        with Journal(self.config, mode="playback") as journal:
            entries = [journal.add_episode(dict(self.video, episode=index), 1, self.config["audio"]["quality"])
                       for index in (1, 2, 3)]
            for entry, device in zip(entries, ("cpu", "gpu")):
                journal.update(entry, "verifying", device=device)
                journal.update(entry, "checking")
                journal.update(entry, "completed", status="completed")
            for live in (True, False):
                data = journal.snapshot(live=live)["runs"][-1]["entries"]
                self.assertEqual([entry["processing"].get("device") for entry in data], ["cpu", "gpu", None])
                self.assertIn("验证设备：", journal.render(live=live))


if __name__ == "__main__":
    unittest.main()
