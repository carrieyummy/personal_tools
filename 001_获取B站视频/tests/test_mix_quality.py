"""验证混合画质目录、命名、双向迁移、断点隔离和画质上限，不访问原站或真实下载目录。"""

from copy import deepcopy
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import yaml

from bilibili_downloader import config, errors, library, media, metadata, models, naming, provenance, quality, runtime
from bilibili_downloader import downloader as transfer
from bilibili_downloader import merge
from bilibili_downloader.merged_records import MergedVideoInfo
from tests import CONFIG_PATH


class MixQualityTests(unittest.TestCase):
    """在临时目录运行真实索引和迁移流程，仅替换媒体工具及网络传输。"""

    def setUp(self) -> None:
        """准备明确的分目录基线配置、两种实际画质和媒体模拟；无参数及返回值。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = replace(config.load_settings(CONFIG_PATH), output_dir=self.root,
                                mix_quality=False, overwrite_merged=False)
        self.bvid = 'BV1xx411c7mD'
        self.choices = [models.Quality(126, '杜比视界'), models.Quality(120, '4K 超高清')]
        self.enterContext(patch.object(metadata, 'probe_media', return_value={'duration_seconds': 1, 'errors': []}))
        self.process = self.enterContext(patch.object(runtime, 'run_process', side_effect=self.concatenate))

    def concatenate(self, command: list[str], **options: object) -> subprocess.CompletedProcess:
        """为 command 写入合并结果；options 为兼容进程参数，返回成功的进程状态。"""
        Path(command[-1]).write_bytes(b'merged-media')
        return subprocess.CompletedProcess(command, 0, '', '')

    def fixture(self, *, same_part: bool = False, shared: bool = False,
                bvid: str | None = None) -> tuple[metadata.VideoInfo, list[Path]]:
        """创建两画质源媒体、字幕和断点；same_part 共用P1，shared 使用合集，bvid 覆盖投稿，返回视图和源路径。"""
        bvid = bvid or self.bvid
        base = self.root / ('测试合集_合集123' if shared else f'投稿_{bvid}')
        record = metadata.VideoInfo(self.root, base, bvid, '投稿', 1 if same_part else 2, shared=shared)
        record.data.update(errors=[], uploader={'id': 1, 'name': 'test'},
                           current_parts=[{'part': part, 'cid': 100 + part} for part in range(1, (1 if same_part else 2) + 1)])
        paths = []
        for number, choice in enumerate(self.choices, 1):
            part = 1 if same_part else number
            folder = record.quality_directory(choice)
            folder.mkdir(parents=True, exist_ok=True)
            stem = f'曲目_qn120_原名_{bvid}_p{part}'
            path = folder / (stem + '.mp4')
            path.write_bytes(f'video-{choice.code}'.encode())
            path.with_suffix('.zh.srt').write_bytes(b'subtitle')
            item = record.entry(f'{bvid}_p{part}', part=part, title='曲目_qn120_原名', quality=choice)
            formats = [{'format_id': str(choice.code), 'vcodec': 'hevc', 'acodec': 'aac', 'width': 3840, 'height': 2160, 'fps': 60}]
            item.update(formats=formats, source_cid=100 + part, content_fingerprint=provenance.file_fingerprint(path))
            record.record_file(item, path)
            stream = folder / '.streams' / f'{bvid}_p{part}' / provenance.stream_version(100 + part, formats)
            stream.mkdir(parents=True)
            (stream / '0.raw.part').write_bytes(f'partial-{choice.code}'.encode())
            paths.append(path)
        record.save()
        return record, paths

    def test_modes_roundtrip_sources_subtitles_streams_and_merges(self) -> None:
        """验证源视频、字幕、断点及成品往返迁移，基础名含qn字样也准确恢复且指纹和文件标识不变；无返回值。"""
        record, originals = self.fixture()
        original_stats = [(path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_size) for path in originals]
        fingerprints = {item['id']: deepcopy(item['content_fingerprint']) for item in record.data['files']}
        merged_paths = [merge.merge_parts(self.settings, 'unused', record, [part, part], quality=choice)
                        for part, choice in enumerate(self.choices, 1)]
        snapshots = {}
        for choice in self.choices:
            index = MergedVideoInfo(self.root, record.quality_directory(choice, role='merged'))
            snapshots.update({item['id']: item['source_snapshot'] for item in index.data['files']})
        mixed_settings = replace(self.settings, mix_quality=True)
        with patch.object(provenance, 'file_fingerprint', side_effect=AssertionError('迁移不应重算摘要')):
            mixed = library.get_video_info(mixed_settings, self.bvid)
        folder = mixed.base_directory.with_name(mixed.base_directory.name + '_分P')
        self.assertEqual(mixed.index_paths, [folder / 'video_info.json'])
        document = metadata.read_json(mixed.index_paths[0])
        self.assertEqual((document['schema_version'], document['mix_quality']), (8, True))
        self.assertNotIn('quality_id', document)
        self.assertTrue(all(item['path'] == item['file_name'] and Path(item['path']).stem == item['stem'] for item in document['files']))
        for item in mixed.data['files']:
            path = mixed.resolve_file(item)
            self.assertEqual(item['file_name'], path.name)
            self.assertEqual(item['stem'], path.stem)
            choice = models.entry_quality(item)
            self.assertTrue(path.stem.endswith('_' + choice.directory))
            self.assertIn('曲目_qn120_原名', path.name)
            self.assertTrue(path.with_suffix('.zh.srt').is_file())
            self.assertEqual(item['content_fingerprint'], fingerprints[item['id']])
            stream = folder / '.streams' / f"{self.bvid}_p{item['part']}" / f'qn{choice.code}'
            self.assertEqual(len(list(stream.glob('cid*/0.raw.part'))), 1)
        mixed_index = MergedVideoInfo(self.root, mixed.quality_directory(self.choices[0], role='merged'))
        self.assertEqual(len(mixed_index.data['files']), 2)
        self.assertEqual({item['id']: item['source_snapshot'] for item in mixed_index.data['files']}, snapshots)
        self.assertTrue(all(item['path'].endswith('_' + models.entry_quality(item).directory + '.mp4') for item in mixed_index.data['files']))
        separated = library.get_video_info(self.settings, self.bvid)
        self.assertFalse(separated.mix_quality)
        for path, stats in zip(originals, original_stats):
            self.assertTrue(path.is_file())
            self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_size), stats)
            self.assertTrue(path.with_suffix('.zh.srt').is_file())
        self.assertTrue(all(path.is_file() for path in merged_paths))
        self.assertEqual(self.process.call_count, 2)

    def test_same_part_multiple_qualities_share_folder_without_collisions(self) -> None:
        """验证同一P的多个画质共用目录后文件名和断点仍隔离；无返回值。"""
        self.fixture(same_part=True)
        mixed = library.get_video_info(replace(self.settings, mix_quality=True), self.bvid)
        paths = [mixed.resolve_file(item) for item in mixed.data['files']]
        self.assertEqual(len({path.parent for path in paths}), 1)
        self.assertEqual(len({path.name for path in paths}), 2)
        self.assertEqual({item['quality_id'] for item in mixed.data['files']}, {120, 126})
        self.assertEqual(len(list((paths[0].parent / '.streams' / f'{self.bvid}_p1').glob('qn*/cid*/0.raw.part'))), 2)

    def test_collection_access_migrates_members_and_preserves_bv_indexes(self) -> None:
        """验证各成员访问后合集归入公共目录且每BV仍独立JSON，读取另一成员不会丢失先前成员；无返回值。"""
        first, _ = self.fixture(shared=True)
        other = 'BV1yy411c7mD'
        self.fixture(shared=True, bvid=other)
        settings = replace(self.settings, mix_quality=True)
        library.get_video_info(settings, self.bvid)
        library.get_video_info(settings, other)
        folder = first.base_directory.with_name(first.base_directory.name + '_分P')
        self.assertEqual({path.name for path in folder.glob('*_video_info.json')}, {f'{self.bvid}_video_info.json', f'{other}_video_info.json'})
        self.assertEqual(len(list(folder.glob('*.mp4'))), 4)
        self.assertEqual(len(metadata.open_video(self.root, self.bvid).data['files']), 2)

    def test_conflict_rolls_back_source_paths_and_all_indexes(self) -> None:
        """验证切换时目标文件冲突不会覆盖任何媒体或JSON；无返回值。"""
        record, originals = self.fixture()
        indexes = {path: path.read_bytes() for path in record.index_paths}
        mixed = record.base_directory.with_name(record.base_directory.name + '_分P')
        mixed.mkdir()
        conflict = mixed / (originals[0].stem + '_' + self.choices[0].directory + '.mp4')
        conflict.write_bytes(b'conflicting-file')
        with self.assertRaises(errors.ToolError):
            library.get_video_info(replace(self.settings, mix_quality=True), self.bvid)
        self.assertTrue(all(path.is_file() for path in originals))
        self.assertEqual({path: path.read_bytes() for path in indexes}, indexes)
        self.assertEqual(conflict.read_bytes(), b'conflicting-file')

    def test_index_write_failure_and_cancel_roll_back_layout(self) -> None:
        """验证媒体移动后索引写入失败或取消时全部恢复原媒体和索引，允许下次重试；无返回值。"""
        record, paths = self.fixture()
        indexes = {path: path.read_bytes() for path in record.index_paths}
        for failure in (OSError('disk full'), errors.DownloadCancelled('取消')):
            with self.subTest(failure=failure):
                with patch.object(metadata, 'atomic_text', side_effect=failure), self.assertRaises(type(failure)):
                    library.get_video_info(replace(self.settings, mix_quality=True), self.bvid)
                self.assertTrue(all(path.is_file() for path in paths))
                self.assertEqual({path: path.read_bytes() for path in indexes}, indexes)

    def test_existing_download_reuses_after_layout_migration(self) -> None:
        """验证迁移后的同CID同格式媒体可直接复用，不再调用下载器；无返回值。"""
        self.fixture()
        settings = replace(self.settings, mix_quality=True)
        record = library.get_video_info(settings, self.bvid)
        item = next(item for item in record.data['files'] if item['quality_id'] == 120)
        info = {'id': f'{self.bvid}_p2', 'title': '曲目_qn120_原名', 'selected_quality': {'code': 120, 'name': '4K 超高清'},
                'requested_formats': [{**item['formats'][0], 'url': 'https://example.com/video', 'ext': 'mp4'}]}
        downloader = MagicMock()
        downloader.dl.side_effect = AssertionError('已下载文件不应重复传输')
        folder, stem = transfer.save_video(downloader, info, settings, 'unused', '投稿', 2, source_cid=102)
        self.assertEqual(folder, record.resolve_file(item).parent)
        self.assertEqual(stem, record.resolve_file(item).stem)
        downloader.dl.assert_not_called()

    def test_mixed_directory_does_not_relax_merge_quality_constraint(self) -> None:
        """验证混合目录中的不同实际画质仍不能跨档位合并，不调用媒体工具；无返回值。"""
        self.fixture()
        settings = replace(self.settings, mix_quality=True)
        record = library.get_video_info(settings, self.bvid)
        assignments = {item['part']: models.entry_quality(item) for item in record.data['files']}
        self.assertEqual(merge.merge_groups(settings, 'unused', record, '1,2', assignments=assignments), 2)
        self.process.assert_not_called()

    def test_mixed_initial_download_names_video_and_isolates_streams(self) -> None:
        """验证新任务首次下载即使用混合目录和实际画质后缀，断点位于对应qn子目录；无返回值。"""
        settings = replace(self.settings, mix_quality=True)
        downloader = MagicMock()
        observed = []

        def download(filename: str, info: dict) -> tuple[bool, bool]:
            """将 info 的流写到 filename 并记录断点路径，返回传输成功状态。"""
            observed.append(Path(filename))
            Path(filename).write_bytes(b'new-video')
            return True, True

        def mux(executable: str, sources: list[Path], formats: list[dict], folder: Path, stem: str) -> Path:
            """模拟 executable 按 formats 将 sources 封装为 folder/stem，返回新视频路径。"""
            path = folder / (stem + '.mp4')
            path.write_bytes(sources[0].read_bytes())
            return path

        downloader.dl.side_effect = download
        info = {'id': f'{self.bvid}_p1', 'title': '新视频', 'selected_quality': {'code': 120, 'name': '4K 超高清'},
                'requested_formats': [{'format_id': '120', 'vcodec': 'hevc', 'acodec': 'aac', 'url': 'https://example.com/video', 'ext': 'mp4'}]}
        with patch.object(media, 'mux_streams', side_effect=mux):
            folder, stem = transfer.save_video(downloader, info, settings, 'unused', '投稿', 1, source_cid=101)
        self.assertEqual(folder.name, f'投稿_{self.bvid}_分P')
        self.assertTrue(stem.endswith('_qn120_4K_超高清'))
        self.assertIn('qn120', observed[0].parts)
        self.assertTrue((folder / (stem + '.mp4')).is_file())

    def test_refresh_recognizes_actual_quality_suffix_without_stripping_title(self) -> None:
        """验证混合目录补扫新媒体时按BV/P后的画质后缀识别，原题目的qn文字完整保留；无返回值。"""
        self.fixture()
        settings = replace(self.settings, mix_quality=True)
        record = library.get_video_info(settings, self.bvid)
        folder = record.quality_directory(self.choices[1])
        base_stem = f'副本_qn126_原名_{self.bvid}_p1'
        extra = folder / (base_stem + '_' + self.choices[1].directory + '.mp4')
        extra.write_bytes(b'extra-media')
        record.refresh_local('unused')
        loaded = metadata.open_video(self.root, self.bvid)
        item = next(item for item in loaded.data['files'] if item['part'] == 1 and item['quality_id'] == 120)
        self.assertEqual(item['base_stem'], base_stem)
        self.assertEqual(item['quality_dir'], self.choices[1].directory)
        self.assertEqual(loaded.resolve_file(item), extra)

    def test_mixed_group_failure_and_success_share_result_index_and_survive_layout_changes(self) -> None:
        """验证混合画质组失败、同档组成功都记录，目录双向迁移及移动到合集后结果不丢失；无返回值。"""
        record, _ = self.fixture()
        record.organize_qualities(mix_quality=True)
        assignments = {part: choice for part, choice in enumerate(self.choices, 1)}
        self.assertEqual(merge.merge_groups(self.settings, 'unused', record, '1-2;2,2', assignments=assignments), 2)
        index = MergedVideoInfo.for_results(record)
        self.assertEqual(len(index.data['files']), 1)
        results = deepcopy(index.data['merge_results'])
        self.assertEqual([item['status'] for item in results], ['failed', 'completed'])
        self.assertEqual(results[0]['source_parts'], [1, 2])
        self.assertEqual([item['quality_id'] for item in results[0]['part_qualities']], [choice.code for choice in self.choices])
        self.assertIn('不同画质', results[0]['errors'][0])
        for mixed in (False, True):
            record.organize_qualities(mix_quality=mixed)
            self.assertEqual(MergedVideoInfo.for_results(record).data['merge_results'], results)
        record = metadata.move_to_collection(record, self.root / '新集合_合集321')
        moved = MergedVideoInfo.for_results(record)
        self.assertEqual([item['id'] for item in moved.data['merge_results']], [item['id'] for item in results])
        self.assertEqual(moved.data['merge_results'][0]['errors'], results[0]['errors'])
        self.assertEqual(len(moved.data['files']), 1)

    def test_failed_remerge_preserves_completed_file_and_adds_failure_result(self) -> None:
        """验证同一索引中旧成品保持完成，重新合并失败另增失败结果，刷新媒体信息不清除失败历史；无返回值。"""
        record, _ = self.fixture()
        record.organize_qualities(mix_quality=True)
        destination = merge.merge_parts(self.settings, 'unused', record, [1, 1], quality=self.choices[0])
        index = MergedVideoInfo.for_results(record)
        files, original_media = deepcopy(index.data['files']), destination.read_bytes()
        self.process.side_effect = errors.ToolError('模拟合并失败')
        with self.assertRaises(errors.ToolError):
            merge.merge_parts(replace(self.settings, overwrite_merged=True), 'unused', record, [1, 1], quality=self.choices[0])
        index = MergedVideoInfo.for_results(record)
        self.assertEqual(index.data['files'], files)
        self.assertEqual(destination.read_bytes(), original_media)
        self.assertEqual([item['status'] for item in index.data['merge_results']], ['completed', 'failed'])
        failure = deepcopy(index.data['merge_results'][-1])
        index.refresh_local('unused')
        self.assertEqual(MergedVideoInfo.for_results(record).data['merge_results'][-1], failure)

    def test_result_only_index_refreshes_without_source_records_or_login(self) -> None:
        """验证只含失败结果且无源媒体的索引可以独立刷新，不创建源占位记录或触发登录；无返回值。"""
        source = metadata.VideoInfo(self.root, self.root / '独立失败_合集123', self.bvid, '失败投稿', 2)
        merge.record_merge_result(source, status='failed', source_parts=[1, 2], expression='1-2', error='缺少分 P')
        before = deepcopy(MergedVideoInfo.for_results(source).data['merge_results'])
        with patch.object(library.auth, 'authenticated_session', side_effect=AssertionError('无需登录')):
            self.assertEqual(library.refresh_metadata(self.settings, Namespace(cookies=None), 'unused'), 0)
        self.assertEqual(MergedVideoInfo.for_results(source).data['merge_results'], before)
        self.assertEqual(list(self.root.rglob('*_video_info.json')), [])

    def orphan_merges(self) -> tuple[MergedVideoInfo, list[Path]]:
        """构造没有任何源索引的双BV合集成品，返回独立合并索引及媒体路径；无参数。"""
        base = self.root / '独立成品_合集123'
        paths = []
        index = None
        for bvid in (self.bvid, 'BV1yy411c7mD'):
            source = metadata.VideoInfo(self.root, base, bvid, '独立成品', 2)
            folder = source.quality_directory(self.choices[0], role='merged')
            folder.mkdir(parents=True, exist_ok=True)
            index = index or MergedVideoInfo(self.root, folder)
            path = folder / f'独立成品_{bvid}_p1-2.mp4'
            path.write_bytes(b'orphan-merged-media')
            item = index.register(source, {'id': models.quality_entry_id(f'{bvid}:merged_p1-2', self.choices[0]),
                                          'kind': 'merged', 'title': '独立成品', 'status': 'completed', 'errors': [],
                                          'formats': [], 'source_parts': [1, 2],
                                          'source_snapshot': [{'part': 1, 'source_cid': 101, 'quality_id': 126, 'sha256': 'saved-snapshot'}],
                                          'quality_id': 126, 'quality_name': self.choices[0].name,
                                          'quality_dir': self.choices[0].directory}, path)
            index.record_file(item, path)
            paths.append(path)
        index.save()
        return index, paths

    def test_independent_merges_switch_modes_without_recreating_source_records(self) -> None:
        """验证源索引全删后仍可双向整理共享成品目录，不登录、不创建源记录，来源快照和媒体属性保持；无返回值。"""
        index, paths = self.orphan_merges()
        snapshot = {item['id']: deepcopy(item['source_snapshot']) for item in index.data['files']}
        original_stats = [(path.stat().st_ino, path.stat().st_mtime_ns) for path in paths]
        with patch('bilibili_downloader.auth.authenticated_session', side_effect=AssertionError('成品整理不应登录')):
            self.assertEqual(library.refresh_metadata(replace(self.settings, mix_quality=True), Namespace(cookies=None), 'unused'), 0)
            common = self.root / '独立成品_合集123_合并'
            current = MergedVideoInfo(self.root, common)
            self.assertEqual(len(current.data['files']), 2)
            self.assertEqual({item['id']: item['source_snapshot'] for item in current.data['files']}, snapshot)
            self.assertTrue(all('_qn126_' in item['path'] for item in current.data['files']))
            self.assertEqual(library.refresh_metadata(self.settings, Namespace(cookies=None), 'unused'), 0)
        self.assertEqual(metadata.discover_videos(self.root), ([], []))
        self.assertEqual(list(self.root.rglob('*_video_info.json')), [])
        for path, before in zip(paths, original_stats):
            self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), before)

    def test_independent_merge_migration_failure_keeps_old_index_and_files(self) -> None:
        """验证独立成品迁移保存失败时旧索引、媒体和快照全部恢复，不遗留源占位记录；无返回值。"""
        index, paths = self.orphan_merges()
        original = index.path.read_bytes()
        with patch.object(metadata, 'atomic_text', side_effect=OSError('disk full')):
            self.assertEqual(library.refresh_metadata(replace(self.settings, mix_quality=True), Namespace(cookies=None), 'unused'), 2)
        self.assertEqual(index.path.read_bytes(), original)
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertEqual(list(self.root.rglob('*_video_info.json')), [])


class CeilingAndConfigTests(unittest.TestCase):
    """验证配置严格读取和不超过统一上限的选择规则。"""

    def test_mix_quality_configuration_requires_boolean_and_key(self) -> None:
        """从实际配置构造 true/false 及非法值，验证新开关不使用隐藏默认值；无返回值。"""
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding='utf-8-sig'))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config.yaml'
            for value in (False, True, 'false', 0, None):
                with self.subTest(value=value):
                    data['download']['mix_quality'] = value
                    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
                    if type(value) is bool:
                        self.assertIs(config.load_settings(path).mix_quality, value)
                    else:
                        with self.assertRaisesRegex(errors.ToolError, 'mix_quality'):
                            config.load_settings(path)
            del data['download']['mix_quality']
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
            with self.assertRaises(errors.ToolError):
                config.load_settings(path)

    def test_one_ceiling_never_selects_higher_and_can_report_no_lower_option(self) -> None:
        """验证档位并集只选择一次上限，较低档自动选最高可用，只有较高档的P明确失败；无返回值。"""
        choices = {code: models.Quality(code, f'画质{code}') for code in (126, 125, 120, 80)}
        catalogs = {1: {126: choices[126], 120: choices[120]}, 2: {80: choices[80]}, 3: {125: choices[125]}}
        with patch('builtins.input', return_value='3') as prompt:
            ceiling = quality.confirm_ceiling(catalogs)
        self.assertEqual(ceiling.code, 120)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(quality.below_ceiling(catalogs[1], ceiling).code, 120)
        self.assertEqual(quality.below_ceiling(catalogs[2], ceiling).code, 80)
        with self.assertRaisesRegex(errors.ToolError, '上限'):
            quality.below_ceiling(catalogs[3], ceiling)
