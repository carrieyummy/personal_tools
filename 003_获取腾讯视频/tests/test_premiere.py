"""验证整剧首播日期、缓存任务复用及既有目录安全迁移。"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local.configuration import load_config, series_directory_name
from local.migration import migrate_existing, migration_plan
from local.playback.errors import PlaybackError
from local.playback.tasks import premiere_date
from local.premiere import add_cache_dates


class PremiereTests(unittest.TestCase):
    """基于当前配置构造公开响应和临时输出，不访问真实账号或媒体。"""

    def setUp(self):
        """加载实际配置副本并创建隔离输出根目录；无返回值。"""
        self.temp = tempfile.TemporaryDirectory(prefix="tencent_premiere_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = deepcopy(load_config(Path("configs/config.yaml")))
        self.config["output_path"] = self.root
        self.date = "2020-02-03"
        self.settings = self.config["playback"]["catalog"]

    def page(self, value, *, cid="cover", title="合成剧"):
        """用 value、cid、title 构造合成整剧简介；返回页面字典。"""
        return {"CardList": [{"type": self.settings["introduction_card"], "params": {"mod_title": title},
                              "children_list": {"0": {"cards": [{"params": {"title": title, "report.cid": cid, "broadcast_time": value}}]}}}]}

    def test_requires_exact_valid_series_premiere(self):
        """仅使用匹配整剧的有效完整日期；缺失、错误日期和其他专辑被拒绝。"""
        self.assertEqual(premiere_date(self.page(self.date), self.settings, "cover", "合成剧"), self.date)
        for value in (None, "2020", "2020-02-30", "2020-2-3", ""):
            with self.subTest(value=value), self.assertRaises(PlaybackError):
                premiere_date(self.page(value), self.settings, "cover", "合成剧")
        with self.assertRaises(PlaybackError):
            premiere_date(self.page(self.date, cid="another"), self.settings, "cover", "合成剧")

    def test_cache_queries_each_series_once(self):
        """同一缓存专辑只查询一次整剧日期，且不会改变视频标题。"""
        tasks = [{"series_id": "cover", "video_id": f"v{i}", "series": "合成剧", "title": f"第{i}集"} for i in (1, 2)]
        with patch("local.premiere.album_date", return_value=("合成剧", self.date)) as query:
            result, errors = add_cache_dates(self.config, tasks)
        self.assertFalse(errors)
        self.assertEqual(query.call_count, 1)
        self.assertEqual([task["release_date"] for task in result], [self.date, self.date])
        self.assertEqual([task["title"] for task in result], [task["title"] for task in tasks])

    def make_folder(self, title, cid, vid):
        """为 title/cid/vid 创建旧命名的合成目录与索引；返回旧路径和日期目录。"""
        new_name = series_directory_name(self.config, title, cid, self.date)
        separator = next(literal for literal, field, _, _ in __import__("string").Formatter().parse(self.config["naming"]["series_template"]) if field == "title")
        old = self.root / new_name.removeprefix(self.date + separator)
        old.mkdir()
        (old / "episode.mp4").write_bytes(b"synthetic media")
        (old / "video_info.json").write_text(json.dumps({"version": 1, "files": [{"video_id": vid, "file": "episode.mp4"}]}), encoding="utf-8")
        return old, self.root / new_name

    def test_renames_directories_and_updates_history_without_changing_media(self):
        """目录迁移保持视频和索引内容，并同步历史成品路径。"""
        old, new = self.make_folder("合成剧", "cover", "video")
        index = (old / "video_info.json").read_bytes()
        history = {"runs": [{"entries": [{"output": str(old / "episode.mp4"), "detail": str(old / "episode.mp4")}]}]}
        state = self.root / self.config["logging"]["state_file"]
        state.write_text(json.dumps(history), encoding="utf-8")
        with patch("local.migration.album_date", return_value=("合成剧", self.date)):
            result = migrate_existing(self.config)
        self.assertEqual(len(result), 1)
        self.assertFalse(old.exists())
        self.assertEqual((new / "episode.mp4").read_bytes(), b"synthetic media")
        self.assertEqual((new / "video_info.json").read_bytes(), index)
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["runs"][0]["entries"][0]["output"], str(new / "episode.mp4"))

    def test_conflicting_target_stops_before_any_rename(self):
        """目标目录已存在时保留原目录和目标，不合并或覆盖。"""
        old, new = self.make_folder("合成剧", "cover", "video")
        new.mkdir()
        (new / "keep").write_text("keep", encoding="utf-8")
        with patch("local.migration.album_date", return_value=("合成剧", self.date)):
            with self.assertRaises(FileExistsError):
                migrate_existing(self.config)
        self.assertTrue((old / "episode.mp4").is_file())
        self.assertEqual((new / "keep").read_text(encoding="utf-8"), "keep")

    def test_failed_second_move_rolls_back_first(self):
        """第二个目录迁移失败时恢复第一个目录，历史保持原位。"""
        first, first_new = self.make_folder("A剧", "a", "videoa")
        second, second_new = self.make_folder("B剧", "b", "videob")
        rename = Path.rename

        def fail_second(path, target):
            """对第二个旧 path 模拟失败，其余 target 使用真实重命名，返回新路径。"""
            if path == second:
                raise OSError("模拟失败")
            return rename(path, target)

        def lookup(config, cid, vid):
            """按合成 cid 返回对应剧名和日期；config/vid 不访问外部资源。"""
            return ("A剧" if cid == "a" else "B剧"), self.date

        with patch("local.migration.album_date", side_effect=lookup), patch.object(Path, "rename", fail_second):
            with self.assertRaisesRegex(OSError, "模拟失败"):
                migrate_existing(self.config)
        self.assertTrue(first.is_dir() and second.is_dir())
        self.assertFalse(first_new.exists() or second_new.exists())

    def test_prepared_plan_cannot_move_directory_outside_root(self):
        """预检计划的最终目标被替换后，锁内再次检查根目录边界。"""
        old, _ = self.make_folder("合成剧", "cover", "video")
        with patch("local.migration.album_date", return_value=("合成剧", self.date)):
            plan = migration_plan(self.config)
        plan[0]["new"] = self.root.parent / (self.root.name + "_outside")
        with self.assertRaisesRegex(RuntimeError, "迁移前"):
            migrate_existing(self.config, plan=plan)
        self.assertTrue((old / "episode.mp4").is_file())
        self.assertFalse(plan[0]["new"].exists())


if __name__ == "__main__":
    unittest.main()
