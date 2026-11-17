"""验证批次发现、版本缓存、事务、错误隔离以及旧版迁移。"""

from copy import deepcopy
from contextlib import redirect_stdout
import os
from pathlib import Path
from unittest.mock import patch

from qsv_local.configuration import read_config
from qsv_local.batch import run_batch
from qsv_local.discovery import discover
from qsv_local.storage import atomic_json, fingerprint, output_lock, publish, read_json
from tests.support import ProjectCase


class DiscoveryTests(ProjectCase):
    """覆盖目录选择、客户端下载元数据映射与配置边界。"""

    def test_recursive_mapping_and_selection_deduplicate(self):
        """递归发现大小写扩展名，重叠目录选择不重复，目标按当前模板生成；无参数或返回值。"""
        self.source("group/sub/one.QSV")
        self.source("other/two.qsv")
        self.source("group/ignored.txt")
        self.raw["input_path"] = ["group", "group/sub"]
        self.write_config()
        tasks = discover(read_config(self.config_path))
        self.assertEqual([task.relative.as_posix() for task in tasks], ["group/sub/one.QSV"])
        self.assertEqual(tasks[0].target, self.target("group/sub/one.QSV"))

    def test_album_identity_and_episode_order_are_taken_from_record(self):
        """使用记录中的专辑名称、ID 和真实集序，而非扫描顺序；无参数或返回值。"""
        source = self.source("input_dir/source.qsv")
        self.metadata[source].update(AlbumID="7654301", AlbumName="原始剧名", vedioorder="12")
        self.write_downloads()
        task = discover(self.config)[0]
        self.assertEqual(task.target, self.target("input_dir/source.qsv"))
        self.assertEqual(task.metadata, ("7654301", "原始剧名", "12"))

    def test_missing_or_invalid_naming_metadata_is_rejected(self):
        """记录缺失专辑 ID、剧名或正集序时停止映射，不生成猜测名称；无参数或返回值。"""
        source = self.source()
        original = deepcopy(self.metadata[source])
        for field, value in (("AlbumID", ""), ("AlbumID", "../bad"), ("AlbumName", ""),
                             ("vedioorder", "0"), ("vedioorder", "1.5")):
            with self.subTest(field=field, value=value):
                self.metadata[source] = {**original, field: value}
                self.write_downloads()
                with self.assertRaises(ValueError):
                    discover(self.config)

    def test_invalid_selections_are_rejected(self):
        """列表类型、根目录外路径和单文件选择在写出前报错；无参数或返回值。"""
        self.source()
        for selection in ("file.qsv", ["../"], ["missing"], ["series/episode.qsv"]):
            with self.subTest(selection=selection):
                self.raw["input_path"] = selection
                self.write_config()
                with self.assertRaises(ValueError):
                    read_config(self.config_path)
        self.assertFalse(self.output.exists())

    def test_invalid_numbers_and_reserved_log_name_are_rejected(self):
        """严格检查布尔整数混用、无限时间与保留索引名；无参数或返回值。"""
        original = deepcopy(self.raw)
        for section, name, value in (("logging", "port", True), ("logging", "write_retries", -1),
                                     ("runtime", "cancel_poll_interval_seconds", float("inf")),
                                     ("logging", "state_file", "video_info.json")):
            with self.subTest(name=name):
                self.raw = deepcopy(original)
                self.raw[section][name] = value
                self.write_config()
                with self.assertRaises(ValueError):
                    read_config(self.config_path)


class BatchTests(ProjectCase):
    """覆盖成品版本和批量转换的实际持久化行为。"""

    def test_first_run_and_cached_rerun(self):
        """首次转换后再次运行只读缓存，跳过 SDK 及摘要重算；无参数或返回值。"""
        self.source()
        first, converter = self.batch()
        self.assertEqual((first["completed"], first["failed"]), (1, 0), self.console.getvalue())
        converter.assert_called_once()
        with patch("qsv_local.storage.hashlib.sha256", side_effect=AssertionError("不应重算缓存摘要")):
            second, converter = self.batch()
        self.assertEqual(second["reused"], 1)
        converter.assert_not_called()
        data = read_json(self.target().parent / "video_info.json")
        self.assertEqual(len(data["files"]), 1)
        self.assertEqual([item["status"] for item in data["conversion_results"]], ["completed", "reused"])

    def test_same_content_with_changed_timestamp_is_reused(self):
        """修改时间变化但摘要相同仍复用并刷新缓存；无参数或返回值。"""
        source = self.source()
        self.batch()
        stamp = source.stat()
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
        result, converter = self.batch()
        self.assertEqual(result["reused"], 1)
        converter.assert_not_called()
        data = read_json(self.target().parent / "video_info.json")
        self.assertEqual(data["sources"]["series/episode.qsv"]["mtime_ns"], source.stat().st_mtime_ns)

    def test_source_versions_are_preserved_with_timestamp_collision_suffix(self):
        """同源内容变化保留所有版本，同秒转换自动追加序号；无参数或返回值。"""
        source = self.source()
        original, _ = self.batch()
        original_path = Path(original["results"][0]["output"])
        original_bytes = original_path.read_bytes()
        with patch("qsv_local.records.time_tag", return_value="000101_000000"):
            source.write_bytes(b"source-second-version")
            second, _ = self.batch()
            source.write_bytes(b"source-third-version")
            third, _ = self.batch()
        self.assertTrue(second["results"][0]["output"].endswith("_000101_000000.mp4"))
        self.assertTrue(third["results"][0]["output"].endswith("_000101_000000_01.mp4"))
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(len(read_json(original_path.parent / "video_info.json")["files"]), 3)

    def test_return_to_historical_content_reuses_old_version(self):
        """源内容恢复为历史版本时复用对应成品，不重复生成；无参数或返回值。"""
        source = self.source()
        first, _ = self.batch()
        source.write_bytes(b"new-content")
        self.batch()
        source.write_bytes(b"source-one")
        again, converter = self.batch()
        self.assertEqual(again["results"][0]["output"], first["results"][0]["output"])
        converter.assert_not_called()

    def test_changed_output_is_preserved_and_new_output_is_created(self):
        """已登记成品被外部修改时保留现场并生成新版本；无参数或返回值。"""
        self.source()
        first, _ = self.batch()
        original = Path(first["results"][0]["output"])
        original.write_bytes(b"external-edit")
        second, _ = self.batch()
        self.assertEqual(second["completed"], 1)
        self.assertNotEqual(first["results"][0]["output"], second["results"][0]["output"])
        self.assertEqual(original.read_bytes(), b"external-edit")

    def test_missing_output_is_regenerated(self):
        """已登记成品丢失时使用新版本名重建，保留历史记录；无参数或返回值。"""
        self.source()
        first, _ = self.batch()
        Path(first["results"][0]["output"]).unlink()
        second, _ = self.batch()
        self.assertEqual(second["completed"], 1)
        self.assertNotEqual(first["results"][0]["output"], second["results"][0]["output"])

    def test_unknown_mp4_is_a_conflict(self):
        """无成功记录的同名 MP4 不覆盖也不追认；无参数或返回值。"""
        self.source()
        existing = self.target()
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"user-file")
        result, converter = self.batch()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(existing.read_bytes(), b"user-file")
        converter.assert_not_called()

    def test_empty_batch_is_successful(self):
        """没有匹配文件时生成空批次报告并成功结束；无参数或返回值。"""
        result, converter = self.batch()
        self.assertEqual((result["total"], result["failed"]), (0, 0))
        self.assertTrue(Path(result["report"]).is_file())
        converter.assert_not_called()

    def test_broken_directory_index_does_not_block_other_directory(self):
        """损坏目录索引保持原样，其他目录继续转换；无参数或返回值。"""
        self.source("a/one.qsv")
        self.source("b/two.qsv")
        broken = self.target("a/one.qsv").parent / "video_info.json"
        broken.parent.mkdir(parents=True)
        broken.write_text("{broken", encoding="utf-8")
        result, _ = self.batch()
        self.assertEqual((result["completed"], result["failed"]), (1, 1), self.console.getvalue())
        self.assertEqual(broken.read_text(encoding="utf-8"), "{broken")
        self.assertTrue(self.target("b/two.qsv").exists())

    def test_index_write_failure_rolls_back_new_media_and_preserves_history(self):
        """新版本索引保存失败时移除本次成品，旧成品与旧索引保持一致；无参数或返回值。"""
        source = self.source()
        self.batch()
        path = self.target().parent / "video_info.json"
        before, files = path.read_bytes(), list(path.parent.glob("*.mp4"))
        source.write_bytes(b"new-content")
        with patch("qsv_local.records.atomic_json", side_effect=OSError("模拟保存失败")):
            result, _ = self.batch()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob("*.mp4")), files)

    def test_conversion_failure_continues_and_keeps_successful_results(self):
        """单项转换失败后继续处理后续视频，并保留独立失败结果；无参数或返回值。"""
        self.source("series/one.qsv")
        self.source("series/two.qsv")
        original = self.fake_prepare

        def selective(config, source, work, notify, cancel_event=None):
            """指定 one 样本失败；config/source/work/notify/cancel_event 转发到准备替身，返回报告。"""
            if source.stem == "one":
                raise RuntimeError("模拟授权失败")
            return original(config, source, work, notify, cancel_event)

        with patch.object(self, "fake_prepare", side_effect=selective):
            result, _ = self.batch()
        self.assertEqual((result["completed"], result["failed"]), (1, 1))
        data = read_json(self.target("series/one.qsv").parent / "video_info.json")
        self.assertEqual([entry["status"] for entry in data["conversion_results"]], ["failed", "completed"])

    def test_invalid_success_proof_is_not_reused(self):
        """成功证明损坏时不追认或覆盖现有成品，索引原样保留；无参数或返回值。"""
        self.source()
        self.batch()
        path = self.target().parent / "video_info.json"
        data = read_json(path)
        data["files"][0]["verification"]["frames"] = 0
        atomic_json(path, data)
        before = path.read_bytes()
        result, converter = self.batch()
        self.assertEqual(result["failed"], 1)
        converter.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_batch_interrupt_saves_current_item_and_stops_dispatch(self):
        """用户中断时登记当前项，后续项不再启动；无参数或返回值。"""
        self.source("series/one.qsv")
        self.source("series/two.qsv")
        with (redirect_stdout(self.console),
              patch("qsv_local.batch.check_gpu"),
              patch("qsv_local.batch.prepare", side_effect=KeyboardInterrupt()) as converter):
            with self.assertRaises(KeyboardInterrupt):
                run_batch(self.config)
        converter.assert_called_once()
        data = read_json(self.target("series/one.qsv").parent / "video_info.json")
        self.assertEqual(data["conversion_results"][-1]["status"], "interrupted")
        log = read_json(self.output / self.config["logging"]["state_file"])
        self.assertEqual(log["runs"][-1]["status"], "interrupted")


class MigrationTests(ProjectCase):
    """覆盖旧单文件报告的可恢复迁移及版本归档。"""

    def test_verified_old_output_is_moved_and_reused(self):
        """旧 MP4 迁入镜像布局，原位置删除且无需再次转换；无参数或返回值。"""
        source = self.source()
        old, report = self.old_report(source)
        (self.work / "mux.log").write_text("旧日志", encoding="utf-8")
        result, converter = self.batch()
        self.assertEqual((result["migrated"], result["reused"]), (1, 1))
        self.assertFalse(old.exists())
        self.assertFalse((self.work / "report.json").exists())
        converter.assert_not_called()
        new = self.target()
        self.assertEqual(fingerprint(new, self.config["copy_buffer_bytes"])["sha256"], report["output_sha256"])
        data = read_json(new.parent / "video_info.json")
        archive = Path(data["files"][0]["work_dir"])
        self.assertTrue((archive / "report.json").exists())
        self.assertTrue((archive / "mux.log").exists())
        self.assertFalse((self.work / "mux.log").exists())

    def test_migration_preserves_old_version_when_source_has_changed(self):
        """旧成品作为历史版本迁移，已变化的 QSV 另外转换新版本；无参数或返回值。"""
        source = self.source()
        old, _ = self.old_report(source)
        old_bytes = old.read_bytes()
        source.write_bytes(b"new-upload")
        result, converter = self.batch()
        self.assertEqual((result["migrated"], result["completed"]), (1, 1))
        converter.assert_called_once()
        self.assertEqual(self.target().read_bytes(), old_bytes)
        self.assertFalse(old.exists())
        self.assertEqual(len(read_json(self.target().parent / "video_info.json")["files"]), 2)

    def test_migration_retries_after_index_write_failure(self):
        """目标已复制但索引失败时保留旧成品与恢复记录，下次继续完成；无参数或返回值。"""
        source = self.source()
        old, _ = self.old_report(source)
        calls = 0

        def fail_once(path, data):
            """首次保存 path/data 失败，随后恢复原子写入；无返回值。"""
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("模拟迁移索引失败")
            atomic_json(path, data)

        with patch("qsv_local.records.atomic_json", side_effect=fail_once):
            first, _ = self.batch()
        self.assertEqual(first["failed"], 1)
        self.assertTrue(old.exists())
        self.assertTrue((self.work / "report.json").exists())
        second, converter = self.batch()
        self.assertEqual((second["migrated"], second["reused"]), (1, 1))
        converter.assert_not_called()
        self.assertFalse(old.exists())
        self.assertEqual(len(read_json(self.target().parent / "video_info.json")["files"]), 1)

    def test_tampered_old_output_is_not_moved(self):
        """旧成品与原成功摘要不符时保留原位并报告失败；无参数或返回值。"""
        source = self.source()
        old, _ = self.old_report(source)
        old.write_bytes(b"unverified-changes")
        result, converter = self.batch()
        self.assertEqual(result["failed"], 1)
        self.assertTrue(old.exists())
        self.assertFalse(self.target().exists())
        converter.assert_not_called()


class StorageTests(ProjectCase):
    """覆盖不覆盖发布及输出根目录的单写入者约束。"""

    def test_failed_copy_validation_leaves_no_partial_target(self):
        """复制摘要错误时不发布成品，并删除输出临时文件；无参数或返回值。"""
        source = self.source()
        target = self.output / "result.mp4"
        with self.assertRaises(RuntimeError):
            publish(source, target, "0" * 64, self.config["copy_buffer_bytes"])
        self.assertFalse(target.exists())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_publication_does_not_overwrite_existing_file(self):
        """并发出现的同名目标拒绝覆盖，原文件保持不变；无参数或返回值。"""
        source = self.source()
        target = self.output / "result.mp4"
        self.output.mkdir()
        target.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            publish(source, target, fingerprint(source, self.config["copy_buffer_bytes"])["sha256"],
                    self.config["copy_buffer_bytes"])
        self.assertEqual(target.read_bytes(), b"original")

    def test_output_lock_rejects_second_writer_and_releases(self):
        """同一输出根目录不能同时执行两个批次，退出后锁自动释放；无参数或返回值。"""
        with output_lock(self.output):
            with self.assertRaises(RuntimeError):
                with output_lock(self.output):
                    self.fail("不应获得第二把锁")
        with output_lock(self.output):
            self.assertTrue(self.output.is_dir())
