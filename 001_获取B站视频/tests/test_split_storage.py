"""验证分P与合并结果分目录、独立索引、旧布局拆分和跨记录回滚。"""

from copy import deepcopy
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bilibili_downloader import auth, config, library, metadata, models
from bilibili_downloader import merge as merging
from bilibili_downloader.merged_records import MergedVideoInfo
import download_bilibili as cli
from bilibili_downloader import runtime
from tests import PROJECT_DIR


class SplitStorageTests(unittest.TestCase):
    """在临时目录构造真实 v3 混放文件，不访问现有下载或原站。"""

    def setUp(self) -> None:
        """创建临时根目录、画质和工具配置；无参数及返回值。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = replace(config.load_settings(PROJECT_DIR / 'config.yaml'), output_dir=self.root, mix_quality=False)
        self.base = self.root / '演唱会_合集9060222'
        self.quality = models.Quality(127, '8K 超高清')
        self.ids = ['BV1xx411c7mD', 'BV1yy411c7mD']

    def fixture(self, bvid: str, *, shared: bool = True, merged: bool = True) -> metadata.VideoInfo:
        """构造 bvid 的 v3 文件及记录；shared 控制合集记录名，merged 控制是否有合并视频，返回旧记录。"""
        base = self.base if shared else self.root / f'独立投稿_{bvid}'
        folder = base.with_name(base.name + '_' + self.quality.directory)
        folder.mkdir(exist_ok=True)
        record = metadata.VideoInfo(self.root, folder, bvid, '演唱会', 2, shared=shared)
        record.data.update(schema_version=3, directory_base=base.name,
                           uploader={'id': 1, 'name': '测试UP'}, errors=[])
        for part in (1, 2):
            path = folder / f'片段_{bvid}_p{part}.mp4'
            path.write_bytes(f'part{part}'.encode())
            path.with_suffix('.zh.srt').write_bytes(b'subtitle')
            item = record.entry(f'{bvid}_p{part}', part=part, quality=self.quality)
            item.update(formats=[{'vcodec': 'avc1', 'acodec': 'none', 'width': 64, 'height': 64}],
                        duration_seconds=1.0, errors=[])
            record.record_file(item, path)
        streams = folder / '.streams' / f'{bvid}_p2'
        streams.mkdir(parents=True)
        (streams / 'raw.part').write_bytes(b'partial')
        if merged:
            path = folder / f'演唱会_{bvid}_p2_p1_p2.mp4'
            path.write_bytes(b'part2part1part2')
            item = record.entry(f'{bvid}:merged_p2_p1_p2', 'merged', quality=self.quality)
            item.update(formats=deepcopy(record.data['files'][0]['formats']), source_parts=[2, 1, 2],
                        duration_seconds=3.0, processing='stream copy', errors=[])
            record.record_file(item, path)
        record.path.write_text(json.dumps(record.data, ensure_ascii=False), encoding='utf-8')
        return record

    def test_two_posts_share_merge_index_and_source_records_stay_unique(self) -> None:
        """验证多BV混放记录拆分、来源顺序、媒体属性和字幕断点，重复迁移保持索引内容不变；无返回值。"""
        old = [self.fixture(bvid) for bvid in self.ids]
        snapshots = {(record.data['bvid'], item['id']): (record.resolve_file(item).stat().st_ino,
                     record.resolve_file(item).stat().st_size, record.resolve_file(item).stat().st_mtime_ns)
                     for record in old for item in record.data['files']}
        for bvid in self.ids:
            report = StringIO()
            with redirect_stdout(report):
                source = library.get_video_info(self.settings, bvid)
            self.assertNotIn('未登记媒体', report.getvalue())
            self.assertTrue(source.path.parent.name.endswith('_分P'))
            self.assertEqual(source.data['schema_version'], 7)
            self.assertEqual([item['kind'] for item in source.data['files']], ['part', 'part'])
            self.assertTrue((source.path.parent / '.streams' / f'{bvid}_p2/raw.part').is_file())
            for item in source.data['files']:
                self.assertTrue(source.resolve_file(item).with_suffix('.zh.srt').is_file())
                path = source.resolve_file(item)
                self.assertEqual((path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns), snapshots[bvid, item['id']])
        index = MergedVideoInfo(self.root, source.quality_directory(self.quality, role='merged'))
        self.assertEqual([item['bvid'] for item in index.data['files']], self.ids)
        for item in index.data['files']:
            self.assertEqual(item['source_parts'], [2, 1, 2])
            self.assertEqual(item['duration_seconds'], 3.0)
            self.assertEqual(item['uploader']['name'], '测试UP')
            path = index.resolve_file(item)
            self.assertEqual((path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns), snapshots[item['bvid'], item['id']])
        before = index.path.read_bytes()
        for bvid in self.ids:
            library.get_video_info(self.settings, bvid)
        self.assertEqual(index.path.read_bytes(), before)
        self.assertEqual(metadata.discover_videos(self.root), (self.ids, []))
        self.assertFalse(old[0].path.parent.exists())

    def test_index_write_then_source_failure_restores_existing_index(self) -> None:
        """验证共享索引已写入后源记录保存失败时，恢复索引原字节、源记录和全部媒体；无返回值。"""
        self.fixture(self.ids[0])
        source = library.get_video_info(self.settings, self.ids[0])
        index = MergedVideoInfo(self.root, source.quality_directory(self.quality, role='merged'))
        before = index.path.read_bytes()
        old = self.fixture(self.ids[1])
        old_bytes = old.path.read_bytes()
        old_files = [old.resolve_file(item) for item in old.data['files']]
        with patch.object(old, 'save', side_effect=OSError('source disk full')), self.assertRaises(OSError):
            old.organize_qualities()
        self.assertEqual(index.path.read_bytes(), before)
        self.assertEqual(old.path.read_bytes(), old_bytes)
        self.assertTrue(all(path.is_file() for path in old_files))
        self.assertFalse((source.path.parent / f'{self.ids[1]}_video_info.json').exists())
        self.assertEqual(list(self.root.rglob('*.tmp')), [])

    def test_partial_media_move_rolls_back_before_record_updates(self) -> None:
        """验证第二个媒体移动失败时，先前已移动的媒体和字幕回滚且旧JSON不变；无返回值。"""
        old = self.fixture(self.ids[0])
        before = old.path.read_bytes()
        paths = [old.resolve_file(item) for item in old.data['files']]
        rename = Path.rename

        def move(path: Path, target: Path) -> Path:
            """将 path 移到 target，第二个分P模拟无权限；其他移动及回滚正常，返回目标路径。"""
            if path == paths[1]:
                raise PermissionError('locked')
            return rename(path, target)

        with patch.object(Path, 'rename', new=move), self.assertRaises(PermissionError):
            old.organize_qualities()
        self.assertEqual(old.path.read_bytes(), before)
        self.assertTrue(all(path.is_file() for path in paths))

    def test_move_to_collection_combines_separate_indexes(self) -> None:
        """验证已有独立合并索引的投稿迁入合集时保留其他BV结果，源合并索引空后清理；无返回值。"""
        self.fixture(self.ids[0])
        first = library.get_video_info(self.settings, self.ids[0])
        self.fixture(self.ids[1], shared=False)
        second = library.get_video_info(self.settings, self.ids[1])
        old_merge = second.quality_directory(self.quality, role='merged')
        metadata.move_to_collection(second, self.base)
        self.assertEqual(second.path.parent, first.path.parent)
        index = MergedVideoInfo(self.root, first.quality_directory(self.quality, role='merged'))
        self.assertEqual([item['bvid'] for item in index.data['files']], self.ids)
        self.assertTrue(all(index.resolve_file(item).is_file() for item in index.data['files']))
        self.assertFalse(old_merge.exists())

    def test_old_record_delete_failure_restores_both_records_and_media(self) -> None:
        """验证新JSON写入后旧记录删除失败时，两类新记录与媒体全部回滚，旧记录可继续使用；无返回值。"""
        old = self.fixture(self.ids[0])
        before = old.path.read_bytes()
        original_path = old.path
        paths = [old.resolve_file(item) for item in old.data['files']]
        unlink = Path.unlink

        def remove(path: Path, missing_ok: bool = False) -> None:
            """删除 path；仅旧记录模拟被占用，missing_ok 控制缺失处理，无返回值。"""
            if path == original_path:
                raise PermissionError('old record locked')
            unlink(path, missing_ok=missing_ok)

        with patch.object(Path, 'unlink', new=remove), self.assertRaises(PermissionError):
            old.organize_qualities()
        self.assertEqual(original_path.read_bytes(), before)
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertFalse(old.quality_directory(self.quality).exists())
        self.assertFalse(old.quality_directory(self.quality, role='merged').exists())

    def test_corrupt_merge_index_is_rejected_before_ffmpeg(self) -> None:
        """验证合并索引损坏时不覆盖索引、不执行拼接，也不触碰源视频；无返回值。"""
        self.fixture(self.ids[0], merged=False)
        source = library.get_video_info(self.settings, self.ids[0])
        folder = source.quality_directory(self.quality, role='merged')
        folder.mkdir()
        path = folder / 'video_info.json'
        path.write_text('{broken', encoding='utf-8')
        with patch.object(runtime, 'run_process') as ffmpeg, self.assertRaises(metadata.ToolError):
            merging.merge_parts(self.settings, 'ffmpeg', source, quality=self.quality)
        ffmpeg.assert_not_called()
        self.assertEqual(path.read_text(encoding='utf-8'), '{broken')

    def test_refresh_merge_index_without_source_files_or_login(self) -> None:
        """验证删除分P后合并索引仍可补生成实际媒体信息，且不触发登录或生成源记录；无返回值。"""
        self.fixture(self.ids[0])
        source = library.get_video_info(self.settings, self.ids[0])
        merge_folder = source.quality_directory(self.quality, role='merged')
        for path in source.path.parent.rglob('*'):
            if path.is_file():
                path.unlink()
        with patch.object(auth, 'authenticated_session') as login, patch.object(metadata, 'probe_media', return_value={
                'duration_seconds': 3.5, 'width': 64, 'height': 64, 'fps': 5, 'video_codec': 'h264', 'audio_codec': None, 'errors': []}):
            self.assertEqual(library.refresh_metadata(self.settings, cli.parse_args(['--refresh-metadata']), 'ffmpeg'), 0)
        login.assert_not_called()
        index = MergedVideoInfo(self.root, merge_folder)
        self.assertEqual(index.data['files'][0]['duration_seconds'], 3.5)
        self.assertEqual(metadata.discover_videos(self.root), ([], []))

    def test_runtime_index_failure_leaves_no_orphan_result(self) -> None:
        """验证拼接索引保存失败时不留下未登记成品，源分P及原索引均保留；无返回值。"""
        self.fixture(self.ids[0], merged=False)
        source = library.get_video_info(self.settings, self.ids[0])
        folder = source.quality_directory(self.quality, role='merged')

        def concatenate(command: list[str], **options: object) -> subprocess.CompletedProcess:
            """将模拟拼接文件写到 command 的目标，options 为进程选项，返回成功结果。"""
            Path(command[-1]).write_bytes(b'merged')
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch.object(runtime, 'run_process', side_effect=concatenate), patch.object(MergedVideoInfo, 'save', side_effect=OSError('full')), self.assertRaises(OSError):
            merging.merge_parts(self.settings, 'ffmpeg', source, quality=self.quality)
        self.assertEqual(list(folder.glob('*.mp4')), [])
        self.assertFalse((folder / 'video_info.json').exists())
        self.assertTrue(all(source.resolve_file(item).is_file() for item in source.data['files']))


if __name__ == '__main__':
    unittest.main()
