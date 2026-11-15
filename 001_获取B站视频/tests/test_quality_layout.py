"""离线验证清晰度目录后缀布局、v2 嵌套目录迁移和失败回滚。"""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bilibili_downloader import config, errors, library, metadata, models
from tests import CONFIG_PATH


class QualityLayoutTests(unittest.TestCase):
    """使用临时合集模拟多个投稿及多种清晰度，不访问真实下载或原站。"""

    def setUp(self) -> None:
        """创建临时下载根目录、配置及旧合集路径；无参数和返回值。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = replace(config.load_settings(CONFIG_PATH), output_dir=self.root, mix_quality=False)
        self.base = self.root / '演唱会_合集9060222'
        self.bvid = 'BV1xx411c7mD'
        self.high = models.Quality(127, '8K 超高清')
        self.low = models.Quality(80, '1080P 高清')

    def old_record(self, bvid: str, qualities: list[models.Quality]) -> tuple[Path, list[Path]]:
        """构造 bvid 的真实 v2 嵌套记录及 qualities 各分 P；返回旧记录路径和媒体路径列表。"""
        self.base.mkdir(exist_ok=True)
        record = metadata.VideoInfo(self.root, self.base, bvid, '演唱会', len(qualities), shared=True)
        paths = []
        for part, quality in enumerate(qualities, 1):
            folder = self.base / quality.directory
            folder.mkdir(exist_ok=True)
            video = folder / f'片段_{bvid}_p{part}.mp4'
            video.write_bytes(f'{bvid}-{part}'.encode())
            video.with_suffix('.zh.srt').write_text('字幕', encoding='utf-8')
            stream = folder / '.streams' / f'{bvid}_p{part}'
            stream.mkdir(parents=True)
            (stream / '0.raw.part').write_bytes(b'partial')
            item = record.entry(f'{bvid}_p{part}', part=part, quality=quality)
            item['formats'] = [{'quality': quality.code, 'vcodec': 'avc1'}]
            record.record_file(item, video)
            paths.append(video)
        record.data['schema_version'] = 2
        # 直接写入旧结构，避免新版本 save 自动将记录放入后缀目录。
        record.path.write_text(json.dumps(record.data, ensure_ascii=False), encoding='utf-8')
        return record.path, paths

    def test_collection_migrates_all_qualities_and_preserves_unique_records(self) -> None:
        """验证多投稿和多画质按目录拆分索引，字幕与断点同步迁移，旧空目录清除并可重复发现；无返回值。"""
        second = 'BV1yy411c7mD'
        _, paths = self.old_record(self.bvid, [self.high, self.low])
        self.old_record(second, [self.high])
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        for bvid in (self.bvid, second):
            record = library.get_video_info(self.settings, bvid)
            self.assertEqual(record.path.parent, self.root / f'{self.base.name}_{self.high.directory}_分P')
            self.assertEqual(record.data['directory_base'], self.base.name)
            self.assertEqual(record.data['schema_version'], 8)
            for item in record.data['files']:
                video = record.resolve_file(item)
                self.assertEqual(video.parent.parent, self.root)
                self.assertTrue(video.with_suffix('.zh.srt').is_file())
                stream = video.parent / '.streams' / f"{bvid}_p{item['part']}" / '0.raw.part'
                self.assertEqual(stream.read_bytes(), b'partial')
        self.assertFalse(self.base.exists())
        record = library.get_video_info(self.settings, self.bvid)
        after = [(record.resolve_file(item).read_bytes(), record.resolve_file(item).stat().st_mtime_ns) for item in record.data['files']]
        self.assertEqual(after, before)
        self.assertEqual(len(list(self.root.rglob(f'{self.bvid}_video_info.json'))), 2)
        self.assertEqual(metadata.discover_videos(self.root), (sorted([self.bvid, second]), []))
        self.assertEqual(library.get_video_info(self.settings, self.bvid).data['files'], record.data['files'])

    def test_existing_target_preserves_old_record_and_media(self) -> None:
        """验证目标同名时在移动前拒绝，不覆盖已有文件，也不改变旧记录；无返回值。"""
        source_record, paths = self.old_record(self.bvid, [self.high])
        original = source_record.read_bytes()
        target = self.root / f'{self.base.name}_{self.high.directory}_分P' / paths[0].name
        target.parent.mkdir()
        target.write_bytes(b'existing')
        with self.assertRaises(errors.ToolError):
            library.get_video_info(self.settings, self.bvid)
        self.assertEqual(target.read_bytes(), b'existing')
        self.assertEqual(source_record.read_bytes(), original)
        self.assertTrue(paths[0].is_file())

    def test_record_write_failure_rolls_back_layout(self) -> None:
        """验证新记录写入失败时媒体、字幕、断点流和旧 JSON 全部恢复；无返回值。"""
        source_record, paths = self.old_record(self.bvid, [self.high])
        original = source_record.read_bytes()
        with patch.object(metadata, 'atomic_text', side_effect=OSError('disk full')), self.assertRaises(OSError):
            library.get_video_info(self.settings, self.bvid)
        self.assertEqual(source_record.read_bytes(), original)
        self.assertTrue(paths[0].is_file())
        self.assertTrue(paths[0].with_suffix('.zh.srt').is_file())
        self.assertTrue((paths[0].parent / '.streams' / f'{self.bvid}_p1/0.raw.part').is_file())
        self.assertFalse((self.root / f'{self.base.name}_{self.high.directory}_分P').exists())

    def test_refresh_finds_unregistered_media_in_sibling_quality(self) -> None:
        """验证记录位于首个清晰度目录时仍扫描同级其他清晰度，且按目录明确编号登记；无返回值。"""
        self.old_record(self.bvid, [self.high])
        record = library.get_video_info(self.settings, self.bvid)
        folder = record.quality_directory(self.low)
        folder.mkdir()
        video = folder / f'新增_{self.bvid}_p2.mp4'
        video.write_bytes(b'video')
        with patch.object(metadata, 'probe_media', return_value={'video_codec': 'h264', 'errors': []}):
            record.refresh_local('ffmpeg')
        item = next(item for item in record.data['files'] if item.get('part') == 2)
        self.assertEqual(item['quality_id'], 80)
        self.assertEqual(record.resolve_file(item), video)
        self.assertEqual(record.path.parent.name, f'{self.base.name}_{self.high.directory}_分P')


if __name__ == '__main__':
    unittest.main()
