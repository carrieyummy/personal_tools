"""验证全片候选检查点，确保输出目录忙碌后可安全续发且拒绝改写内容。"""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from qsv_local.playback.coordination import StageControls
from qsv_local.playback.publication import publish_validated, resume_validated
from qsv_local.storage import atomic_json, fingerprint


class PublicationTests(unittest.TestCase):
    """使用临时路径和真实摘要，核对发布失败前后的可靠候选。"""

    def setUp(self):
        """读取当前配置并建立合成已验证候选；无返回值。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.config = yaml.safe_load((Path(__file__).resolve().parents[1] / "configs/config.yaml").read_text(encoding="utf-8"))
        self.config["output_path"] = str(self.root / "output")
        self.config["work_dir"] = str(self.work)
        self.controls = StageControls(self.config)
        self.candidate = self.work / "validated.partial.mp4"
        self.candidate.write_bytes(b"synthetic fully verified media")
        self.identity = {"sha256": "synthetic identity"}
        self.video = {"album_id": "1", "title": "合成剧", "episode": 1, "episode_title": "合成剧第1集", "vid": "synthetic", "duration_seconds": 1, "release_date": "2000-01-02"}
        self.report = {"schema_version": 1, "record_type": "playback_download", "source_identity": self.identity, "media_verified": True,
                       "video": self.video,
                       "verification": {"decode_exit_code": 0, "frames": 1},
                       "validated_fingerprint": fingerprint(self.candidate, self.config["copy_buffer_bytes"]), "status": "validated"}
        atomic_json(self.work / "validated.json", self.report)

    def test_busy_output_preserves_proof_and_candidate_for_resume(self):
        """输出锁占用不能丢失已验证候选；恢复时重新核对内容摘要，无返回值。"""
        with patch("qsv_local.playback.publication.require_same_metadata"), patch("qsv_local.playback.publication.output_lock", side_effect=RuntimeError("合成占用")):
            with self.assertRaisesRegex(RuntimeError, "合成占用"):
                publish_validated({"main_config": self.root / "main.yaml"}, self.config, {}, {}, self.work, deepcopy(self.report), self.controls)
        self.assertTrue(self.candidate.exists())
        with patch("qsv_local.playback.publication.probe_media", return_value={"video_codec": "synthetic", "errors": []}):
            recovered = resume_validated(self.work, self.identity, self.video, self.config, self.controls)
        self.assertTrue(recovered["resumed_validation"])
        self.assertEqual(recovered["verification"], self.report["verification"])

    def test_modified_validated_media_is_never_reused(self):
        """检查点存在但内容被修改时必须停止，不继承旧验证成功标记；无返回值。"""
        self.candidate.write_bytes(b"changed after verification")
        with self.assertRaisesRegex(RuntimeError, "发生变化"):
            resume_validated(self.work, self.identity, self.video, self.config, self.controls)

    def test_different_source_cannot_claim_prior_validation(self):
        """其他片源不能接管已有验证证明；无返回值。"""
        with self.assertRaisesRegex(RuntimeError, "来源"):
            resume_validated(self.work, {"sha256": "different source"}, self.video, self.config, self.controls)

    def test_renamed_episode_does_not_relabel_old_candidate(self):
        """命名资料变化后保留旧候选，不能沿用旧证明发布到新命名；无返回值。"""
        with self.assertRaisesRegex(RuntimeError, "命名"):
            resume_validated(self.work, self.identity, {**self.video, "title": "更名后"}, self.config, self.controls)

    def test_changed_release_date_does_not_move_validated_candidate(self):
        """已有日期记录发生变化时停止重新映射，候选仍留在原位；无返回值。"""
        with self.assertRaisesRegex(RuntimeError, "上映日期"):
            resume_validated(self.work, self.identity, {**self.video, "release_date": "2001-01-02"}, self.config, self.controls)
        self.assertTrue(self.candidate.exists())

    def test_pre_date_checkpoint_can_resume_without_reprocessing(self):
        """日期功能启用前的候选保持旧来源与完整摘要校验，增加日期不要求再次解码；无返回值。"""
        old = deepcopy(self.report)
        old["video"].pop("release_date")
        atomic_json(self.work / "validated.json", old)
        with patch("qsv_local.playback.publication.probe_media", return_value={"errors": []}):
            recovered = resume_validated(self.work, self.identity, self.video, self.config, self.controls)
        self.assertTrue(recovered["resumed_validation"])

    def test_publication_cleans_retired_and_partial_media_only_after_index_commit(self):
        """验证成功发布后清理旧媒体，配置保留中间文件时保留，报告及其他文件始终保留。"""
        for keep in (False, True):
            with self.subTest(keep_intermediates=keep):
                self.config["keep_intermediates"] = keep
                self.candidate.write_bytes(b"synthetic fully verified media")
                names = ["outer.ts", "clear_00_video.mp4.retired_20261119_011020_123456",
                         "validated.partial.mp4.retired_20261119_011020_123456",
                         "segment_00001.ts.retired_20261119_011020_123456",
                         "segment_00002.ts." + "a" * 32 + ".partial"]
                for name in names:
                    (self.work / name).write_bytes(b"old media")
                preserved = ["full_report.json.retired_20261119_011020_123456", "segment_notes.ts", "unrelated.mp4"]
                for name in preserved:
                    (self.work / name).write_bytes(b"preserve")
                target = (self.root / "output" / ("kept.mp4" if keep else "cleaned.mp4")).resolve()
                target.parent.mkdir(exist_ok=True)
                with patch("qsv_local.playback.publication.require_same_metadata"), patch("qsv_local.playback.publication.target_path", return_value=target):
                    result = publish_validated({"main_config": self.root / "main.yaml"}, self.config, self.video, {}, self.work, deepcopy(self.report), self.controls)
                self.assertEqual(result["status"], "completed")
                self.assertTrue(target.exists())
                self.assertEqual(self.candidate.exists(), keep)
                for name in names:
                    self.assertEqual((self.work / name).exists(), keep, name)
                for name in preserved:
                    self.assertTrue((self.work / name).exists(), name)

    def test_index_failure_keeps_retired_media(self):
        """索引提交失败时保留当前候选和旧媒体，供后续恢复；无返回值。"""
        self.config["keep_intermediates"] = False
        retired = self.work / "outer.ts.retired_20261119_011020_123456"
        retired.write_bytes(b"old media")
        target = (self.root / "output" / "failed.mp4").resolve()
        target.parent.mkdir(exist_ok=True)
        with patch("qsv_local.playback.publication.require_same_metadata"), patch("qsv_local.playback.publication.target_path", return_value=target), patch("qsv_local.playback.publication.PlaybackDirectoryIndex.save", side_effect=OSError("index failed")):
            with self.assertRaisesRegex(OSError, "index failed"):
                publish_validated({"main_config": self.root / "main.yaml"}, self.config, self.video, {}, self.work, deepcopy(self.report), self.controls)
        self.assertTrue(retired.exists())
        self.assertTrue(self.candidate.exists())
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
