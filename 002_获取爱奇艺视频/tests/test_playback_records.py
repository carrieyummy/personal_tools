"""验证链接目录索引的并发发布、旧记录合并、QSV 共存及失败恢复。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from qsv_local.playback.coordination import StageControls
from qsv_local.playback.outputs import target_path
from qsv_local.playback.publication import publish_validated
from qsv_local.playback.records import PlaybackDirectoryIndex, consolidate_output
from qsv_local.records import DirectoryIndex
from qsv_local.storage import atomic_json, fingerprint, read_json


class PlaybackRecordsTests(unittest.TestCase):
    """使用配置派生的临时目录和真实指纹，检查元数据合并与提交边界。"""

    def setUp(self) -> None:
        """读取当前主配置并建立隔离输出、工作目录及合成视频资料；无参数及返回值。"""
        temporary = tempfile.TemporaryDirectory(prefix="playback-index-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        project = Path(__file__).resolve().parents[1]
        self.config = yaml.safe_load((project / "configs/config.yaml").read_text(encoding="utf-8"))
        self.output = self.root / "output"
        self.config.update(output_path=str(self.output), work_dir=str(self.root / "work"), keep_intermediates=True)
        self.profile = {"main_config": self.root / "main.yaml"}
        self.video = {"album_id": "123", "title": "合成剧", "episode": 1,
                      "episode_title": "合成剧第1集", "release_date": "2000-01-02"}
        self.source = {"scrsz": "16x16", "dr": 0}
        self.controls = StageControls(self.config)
        self.folder = target_path(self.config, self.profile["main_config"], self.video, self.source).parent

    def completed_record(self, order: int) -> tuple[Path, dict]:
        """在本剧目录创建集序 order 的合成成品及完整成功证明；返回媒体路径和报告。"""
        video = {**self.video, "episode": order, "episode_title": f"合成剧第{order}集"}
        target = target_path(self.config, self.profile["main_config"], video, self.source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"synthetic verified episode {order}".encode("utf-8"))
        return target, {"schema_version": 1, "record_type": "playback_download", "status": "completed",
                        "source_identity": {"tvid": str(order)}, "video": video, "output": str(target),
                        "media_verified": True, "verification": {"decode_exit_code": 0, "frames": 1},
                        "output_fingerprint": fingerprint(target, self.config["copy_buffer_bytes"])}

    def old_record(self, order: int) -> tuple[Path, dict]:
        """为集序 order 生成旧逐集 JSON；返回旧记录路径和完整报告。"""
        target, report = self.completed_record(order)
        path = target.with_suffix(".playback.json")
        atomic_json(path, report)
        return path, report

    def test_concurrent_episodes_share_one_index_without_lost_updates(self) -> None:
        """多集共用发布槽后只保留一个 JSON，所有集的成功证明均被提交；无参数及返回值。"""
        jobs = []
        for order in (1, 2, 3):
            target, report = self.completed_record(order)
            work = self.root / "work" / str(order)
            work.mkdir(parents=True)
            candidate = work / "validated.partial.mp4"
            target.rename(candidate)
            report.update(status="validated", validated_fingerprint=fingerprint(candidate, self.config["copy_buffer_bytes"]))
            jobs.append((work, report))
        with patch("qsv_local.playback.publication.require_same_metadata"), ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(publish_validated, self.profile, self.config, report["video"], self.source,
                                   work, report, self.controls) for work, report in jobs]
            results = [future.result() for future in futures]
        index = PlaybackDirectoryIndex(self.folder, self.output)
        self.assertEqual(set(index.records()), {Path(report["output"]).name for report in results})
        self.assertEqual(list(self.folder.glob("*.json")), [index.path])
        self.assertEqual(len(list(self.folder.glob("*.mp4"))), len(jobs))

    def test_concurrent_movies_share_root_index(self) -> None:
        """多个电影直接发布至输出根目录时共享索引且完整保留全部记录。"""
        jobs = []
        for order in (1, 2):
            video = {**self.video, "is_movie": True, "album_id": str(order), "title": f"影片{order}"}
            work = self.root / "work" / str(order)
            work.mkdir(parents=True)
            candidate = work / "validated.partial.mp4"
            candidate.write_bytes(f"movie {order}".encode())
            report = {"schema_version": 1, "record_type": "playback_download", "status": "validated",
                      "source_identity": {"tvid": str(order)}, "video": video, "media_verified": True,
                      "verification": {"decode_exit_code": 0, "frames": 1},
                      "validated_fingerprint": fingerprint(candidate, self.config["copy_buffer_bytes"])}
            jobs.append((work, report))
        with patch("qsv_local.playback.publication.require_same_metadata"), ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(publish_validated, self.profile, self.config, report["video"], self.source,
                                   work, report, self.controls) for work, report in jobs]
            results = [future.result() for future in futures]
        index = PlaybackDirectoryIndex(self.output, self.output)
        self.assertEqual(set(index.records()), {Path(report["output"]).name for report in results})
        self.assertTrue(all(Path(report["output"]).parent == self.output for report in results))

    def test_merge_preserves_qsv_records_and_media_and_is_idempotent(self) -> None:
        """旧 JSON 全量合并并清理，QSV 分区和视频内容、时间戳保持原样，重复整理无变化；无参数及返回值。"""
        old, report = self.old_record(1)
        other, second = self.old_record(2)
        media = Path(report["output"])
        before = (media.read_bytes(), media.stat().st_mtime_ns)
        index = DirectoryIndex(self.folder, self.output)
        qsv = self.folder / "qsv.mp4"
        qsv.write_bytes(b"synthetic verified qsv")
        qsv_fp = fingerprint(qsv, self.config["copy_buffer_bytes"])
        index.add_file("synthetic.qsv", qsv_fp, qsv, qsv_fp, {"verification": report["verification"]}, {})
        index.data["sources"]["synthetic.qsv"] = qsv_fp
        index.save()
        qsv_data = deepcopy(index.data)
        merged = consolidate_output(self.output, self.config)
        data = read_json(index.path)
        self.assertEqual(sum(merged.values()), 2)
        self.assertEqual(data["playback_files"], {media.name: report, Path(second["output"]).name: second})
        for key in ("sources", "files", "conversion_results", "created_at"):
            self.assertEqual(data[key], qsv_data[key])
        self.assertFalse(old.exists())
        self.assertFalse(other.exists())
        self.assertEqual((media.read_bytes(), media.stat().st_mtime_ns), before)
        saved = index.path.read_bytes()
        self.assertEqual(consolidate_output(self.output, self.config), {})
        self.assertEqual(index.path.read_bytes(), saved)
        # QSV 后续提交仍须保留同一文件中的链接分区。
        DirectoryIndex(self.folder, self.output).save()
        self.assertEqual(read_json(index.path)["playback_files"], data["playback_files"])

    def test_failed_index_write_preserves_old_records_for_retry(self) -> None:
        """合并写盘失败时不删除旧记录，恢复写盘后可完成整理；无参数及返回值。"""
        old, report = self.old_record(1)
        before = old.read_bytes()
        with patch("qsv_local.records.atomic_json", side_effect=OSError("合成索引写盘失败")):
            with self.assertRaisesRegex(OSError, "写盘失败"):
                consolidate_output(self.output, self.config)
        self.assertEqual(old.read_bytes(), before)
        self.assertFalse((self.folder / "video_info.json").exists())
        self.assertEqual(sum(consolidate_output(self.output, self.config).values()), 1)
        self.assertEqual(read_json(self.folder / "video_info.json")["playback_files"][Path(report["output"]).name], report)

    def test_conflicting_old_record_is_never_deleted(self) -> None:
        """已存在的同名索引与旧记录冲突时，保留索引和旧 JSON；无参数及返回值。"""
        old, report = self.old_record(1)
        index = PlaybackDirectoryIndex(self.folder, self.output)
        index.add(Path(report["output"]).name, {**report, "highest_audio_verified": True})
        index.save()
        before = index.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "冲突"):
            consolidate_output(self.output, self.config)
        self.assertTrue(old.exists())
        self.assertEqual(index.path.read_bytes(), before)

    def test_malformed_proof_missing_media_and_escaping_path_preserve_records(self) -> None:
        """缺少全片证明、媒体缺失或路径越界时整目录停止合并，保留全部旧 JSON；无参数及返回值。"""
        first, _ = self.old_record(1)
        second, report = self.old_record(2)
        for changed in ({**report, "media_verified": False}, {**report, "output": str(self.root / "outside.mp4")}):
            with self.subTest(changed=changed["output"], verified=changed["media_verified"]):
                atomic_json(second, changed)
                with self.assertRaises(ValueError):
                    consolidate_output(self.output, self.config)
                self.assertTrue(first.exists())
                self.assertTrue(second.exists())
                self.assertFalse((self.folder / "video_info.json").exists())
        atomic_json(second, report)
        Path(report["output"]).unlink()
        with self.assertRaisesRegex(ValueError, "缺失"):
            consolidate_output(self.output, self.config)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_interrupted_cleanup_can_resume_from_committed_index(self) -> None:
        """索引提交成功但删除旧 JSON 失败后，重试可核对相同记录并完成清理；无参数及返回值。"""
        old, report = self.old_record(1)
        unlink = Path.unlink

        def fail_old(path: Path, *args, **kwargs):
            """仅阻止旧记录 path 删除；args/kwargs 透传其余文件操作，返回原 unlink 结果。"""
            if path == old:
                raise PermissionError("合成清理占用")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_old):
            with self.assertRaisesRegex(PermissionError, "清理占用"):
                consolidate_output(self.output, self.config)
        self.assertTrue(old.exists())
        self.assertEqual(read_json(self.folder / "video_info.json")["playback_files"][Path(report["output"]).name], report)
        self.assertEqual(sum(consolidate_output(self.output, self.config).values()), 1)

    def test_failed_publication_index_removes_only_new_copy_and_keeps_candidate(self) -> None:
        """发布索引失败时回收新副本并保留候选，已有成品及旧索引保持原样；无参数及返回值。"""
        _, saved = self.completed_record(1)
        index = PlaybackDirectoryIndex(self.folder, self.output)
        index.add(Path(saved["output"]).name, saved)
        index.save()
        before = index.path.read_bytes()
        target, report = self.completed_record(2)
        work = self.root / "work"
        work.mkdir()
        candidate = work / "validated.partial.mp4"
        target.rename(candidate)
        report.update(status="validated", validated_fingerprint=fingerprint(candidate, self.config["copy_buffer_bytes"]))
        with patch("qsv_local.playback.publication.require_same_metadata"), patch(
                "qsv_local.records.atomic_json", side_effect=OSError("合成提交失败")):
            with self.assertRaisesRegex(OSError, "提交失败"):
                publish_validated(self.profile, self.config, report["video"], self.source, work, report, self.controls)
        self.assertFalse(target.exists())
        self.assertTrue(candidate.exists())
        self.assertTrue(Path(saved["output"]).exists())
        self.assertEqual(index.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
