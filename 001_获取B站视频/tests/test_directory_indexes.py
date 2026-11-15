"""验证源索引按目录拆分、合集按 BV 保存、多索引读写事务及独立目录读取。"""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bilibili_downloader import auth, config, library, metadata, models, naming, provenance, runtime
from bilibili_downloader import merge
from bilibili_downloader import downloader as transfer
from bilibili_downloader.errors import ToolError
from bilibili_downloader.storage import apply_changes
from tests import CONFIG_PATH


class DirectoryIndexTests(unittest.TestCase):
    """使用隔离媒体和真实 JSON 检查新目录索引，迁移过程不访问网络或媒体工具。"""

    def setUp(self) -> None:
        """建立临时根目录、真实项目配置和三个画质的媒体夹具；无参数及返回值。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = replace(config.load_settings(CONFIG_PATH), output_dir=self.root)
        self.bvid = 'BV1xx411c7mD'
        self.qualities = [models.Quality(126, '杜比视界'), models.Quality(125, 'HDR 真彩'), models.Quality(120, '4K 超高清')]

    def fixture(self, bvid: str | None = None, *, shared: bool = False, legacy: bool = True) -> metadata.VideoInfo:
        """建立 bvid 的三个本地分 P；shared 使用合集 BV 文件名，legacy 构造旧 v4 总索引，返回已保存的视图。"""
        bvid = bvid or self.bvid
        base = self.root / ('测试合集_合集123' if shared else f'投稿_{bvid}')
        folder = base.with_name(base.name + '_' + self.qualities[0].directory + '_分P')
        record = metadata.VideoInfo(self.root, folder, bvid, '投稿', 3, shared=shared)
        record.data.update(schema_version=4 if legacy else 5, directory_base=base.name, errors=[],
                           current_parts=[{'part': part, 'cid': 100 + part} for part in range(1, 4)])
        for part, quality in enumerate(self.qualities, 1):
            parent = base.with_name(base.name + '_' + quality.directory + '_分P')
            parent.mkdir(parents=True, exist_ok=True)
            path = parent / (naming.video_stem(self.settings, f'片段{part}', f'{bvid}_p{part}') + '.mp4')
            path.write_bytes(f'video-{bvid}-{part}'.encode())
            item = record.entry(f'{bvid}_p{part}', part=part, quality=quality)
            item.update(source_cid=100 + part, formats=[{'vcodec': 'hevc', 'acodec': 'aac', 'width': 3840, 'height': 2160}],
                        content_fingerprint=provenance.file_fingerprint(path))
            record.record_file(item, path)
        record.save()
        return record

    def test_legacy_split_preserves_media_and_provenance(self) -> None:
        """验证旧总索引原地拆成三个本地 JSON，元数据、CID 和摘要不变且迁移幂等；无返回值。"""
        record = self.fixture()
        originals = {record.resolve_file(item): record.resolve_file(item).stat() for item in record.data['files']}
        fingerprints = {item['id']: deepcopy(item['content_fingerprint']) for item in record.data['files']}
        with patch.object(auth, 'authenticated_session', side_effect=AssertionError('不应联网')), \
                patch.object(runtime, 'run_process', side_effect=AssertionError('不应操作媒体')), \
                patch.object(provenance, 'file_fingerprint', side_effect=AssertionError('不应重新计算指纹')):
            record.migrate_directory_indexes()
        self.assertEqual(len(record.index_paths), 3)
        for path in record.index_paths:
            data = metadata.read_json(path)
            self.assertEqual((data['schema_version'], data['record_type']), (8, 'source'))
            self.assertEqual(len(data['files']), 1)
            item = data['files'][0]
            self.assertEqual(item['path'], item['file_name'])
            self.assertEqual(item['content_fingerprint'], fingerprints[item['id']])
            self.assertEqual(item['source_cid'], 100 + item['part'])
        loaded = metadata.open_video(self.root, self.bvid)
        loaded.migrate_directory_indexes()
        self.assertEqual(len(loaded.data['files']), 3)
        self.assertEqual(len(list(self.root.rglob('video_info.json'))), 3)
        for path, before in originals.items():
            after = path.stat()
            self.assertEqual((after.st_size, after.st_mtime_ns, after.st_ino), (before.st_size, before.st_mtime_ns, before.st_ino))

    def test_new_source_save_and_metadata_update_write_local_entries_only(self) -> None:
        """验证新建即按目录保存，投稿信息更新覆盖各索引而各自文件列表互不混入；无返回值。"""
        record = self.fixture(legacy=False)
        loaded = metadata.open_video(self.root, self.bvid)
        loaded.data['title'] = '修改后的标题'
        loaded.data['current_parts'][0]['cid'] = 201
        loaded.save()
        for path in loaded.index_paths:
            data = metadata.read_json(path)
            self.assertEqual(data['title'], '修改后的标题')
            self.assertEqual(data['current_parts'][0]['cid'], 201)
            self.assertEqual(len(data['files']), 1)
            self.assertTrue((path.parent / data['files'][0]['path']).is_file())
        self.assertEqual(len(record.index_paths), 3)

    def test_pending_source_save_removes_empty_placeholder_directory(self) -> None:
        """验证投稿和番剧登记分 P 后删除空占位目录，两种画质布局均保留目标索引及下载根目录；无返回值。"""
        for identifier in (self.bvid, 'ss123'):
            for mixed in (False, True):
                with self.subTest(source=identifier, mix_quality=mixed):
                    root = self.root / f'{identifier}_{mixed}'
                    settings = replace(self.settings, output_dir=root, mix_quality=mixed)
                    record = library.get_video_info(settings, identifier, '测试来源', 1)
                    base, old_path = record.base_directory, record.path
                    record.save()
                    self.assertTrue(old_path.is_file())
                    record.entry(f'{identifier}_p1', part=1, quality=self.qualities[0])
                    record.save()
                    self.assertFalse(base.exists())
                    self.assertTrue(root.is_dir())
                    self.assertTrue(record.path.is_file())
                    self.assertEqual(record.path.parent, record.quality_directory(self.qualities[0]))
                    self.assertEqual(len(metadata.open_video(root, identifier).data['files']), 1)

    def test_placeholder_cleanup_preserves_shared_records_and_unfinished_files(self) -> None:
        """验证占位索引迁出后保留其他投稿的索引、媒体、字幕和断点，不把非空目录作为残留删除；无返回值。"""
        base = self.root / '测试合集_合集123'
        record = metadata.VideoInfo(self.root, base, self.bvid, '测试合集', 1, shared=True)
        record.data['mix_quality'] = self.settings.mix_quality
        record.save()
        other = metadata.VideoInfo(self.root, base, 'BV1yy411c7mD', '测试合集', 1, shared=True)
        other.save()
        retained = {other.path: other.path.read_bytes(), base / '未登记视频.mp4': b'media',
                    base / '未登记视频.zh.srt': b'subtitle', base / '.streams' / 'other_source' / 'raw.part': b'partial'}
        for path, content in retained.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        old_path = record.path
        record.entry(f'{self.bvid}_p1', part=1, quality=self.qualities[0])
        record.save()
        self.assertFalse(old_path.exists())
        self.assertTrue(base.is_dir())
        self.assertEqual({path: path.read_bytes() for path in retained}, retained)
        self.assertTrue(record.path.is_file())

    def test_failed_pending_save_preserves_placeholder_and_can_retry(self) -> None:
        """验证新索引写入失败时保留旧占位索引与目录，恢复后重试成功才清理旧目录；无返回值。"""
        record = library.get_video_info(self.settings, self.bvid, '测试来源', 1)
        record.save()
        base, old_path = record.base_directory, record.path
        original = old_path.read_bytes()
        record.entry(f'{self.bvid}_p1', part=1, quality=self.qualities[0])
        target = record.quality_directory(self.qualities[0])
        with patch.object(metadata, 'atomic_text', side_effect=OSError('disk full')), self.assertRaises(OSError):
            record.save()
        self.assertEqual(old_path.read_bytes(), original)
        self.assertFalse(target.exists())
        record.save()
        self.assertFalse(base.exists())
        self.assertTrue(record.path.is_file())

    def test_nested_save_defers_cleanup_until_outer_transaction_succeeds(self) -> None:
        """验证内层索引迁移后外层失败仍能恢复旧目录与原索引，后续独立事务不受待清理状态影响；无返回值。"""
        record = library.get_video_info(self.settings, self.bvid, '测试来源', 1)
        record.save()
        base, old_path = record.base_directory, record.path
        original = old_path.read_bytes()
        record.entry(f'{self.bvid}_p1', part=1, quality=self.qualities[0])
        new_path = record.quality_directory(self.qualities[0]) / old_path.name

        def commit() -> None:
            """保存内层索引后模拟外层提交失败，验证回滚前旧空目录仍保留；无参数及返回值。"""
            record.save()
            self.assertFalse(old_path.exists())
            self.assertTrue(base.is_dir())
            raise OSError('outer commit failed')

        with self.assertRaisesRegex(OSError, 'outer commit failed'):
            apply_changes(self.root, [], record.index_paths, commit)
        self.assertEqual(old_path.read_bytes(), original)
        self.assertFalse(new_path.exists())
        record = metadata.open_video(self.root, self.bvid)
        record.entry(f'{self.bvid}_p1', part=1, quality=self.qualities[0])
        record.save()
        self.assertFalse(base.exists())
        self.assertTrue(record.path.is_file())

    def test_unchanged_layout_cleans_empty_residue_without_rewriting_index(self) -> None:
        """验证布局不变时也清理旧基础目录和空画质目录，保留现有索引字节、修改时间及无关目录；无返回值。"""
        record = library.get_video_info(self.settings, self.bvid, '测试来源', 1)
        record.entry(f'{self.bvid}_p1', part=1, quality=self.qualities[0])
        record.save()
        base = record.base_directory
        base.mkdir()
        empty_quality = base.with_name(base.name + '_' + self.qualities[1].directory + '_分P')
        empty_quality.mkdir()
        unrelated = self.root / '无关空目录'
        unrelated.mkdir()
        original, mtime = record.path.read_bytes(), record.path.stat().st_mtime_ns
        record.organize_qualities(mix_quality=self.settings.mix_quality)
        self.assertFalse(base.exists())
        self.assertFalse(empty_quality.exists())
        self.assertTrue(unrelated.is_dir())
        self.assertEqual(record.path.read_bytes(), original)
        self.assertEqual(record.path.stat().st_mtime_ns, mtime)

    def test_unchanged_layout_preserves_nonempty_base_directory(self) -> None:
        """验证布局不变时旧基础目录中未登记文件和其他来源断点均保留；无返回值。"""
        record = library.get_video_info(self.settings, self.bvid, '测试来源', 1)
        record.entry(f'{self.bvid}_p1', part=1, quality=self.qualities[0])
        record.save()
        base = record.base_directory
        retained = {base / '保留说明.txt': b'note', base / '.streams' / 'other_source' / 'raw.part': b'partial'}
        for path, content in retained.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        original = record.path.read_bytes()
        record.organize_qualities(mix_quality=self.settings.mix_quality)
        self.assertEqual({path: path.read_bytes() for path in retained}, retained)
        self.assertEqual(record.path.read_bytes(), original)

    def test_reopening_source_preserves_empty_active_stream_directories(self) -> None:
        """验证基础目录已清理后 BV/ss/ep 两种布局的空断点结构仍可写入，不误判为旧单 P 断点；无返回值。"""
        for identifier in (self.bvid, 'ss123', 'ep456'):
            for mixed in (False, True):
                with self.subTest(source=identifier, mix_quality=mixed):
                    root = self.root / f'active_{identifier}_{mixed}'
                    settings = replace(self.settings, output_dir=root, mix_quality=mixed)
                    record = library.get_video_info(settings, identifier, '测试来源', 1)
                    record.entry(f'{identifier}_p1', part=1, quality=self.qualities[0])
                    record.save()
                    base = record.base_directory
                    staging = record.path.parent / '.streams' / f'{identifier}_p1'
                    if mixed:
                        staging /= f'qn{self.qualities[0].code}'
                    staging /= 'cid123_pending'
                    staging.mkdir(parents=True)
                    base.mkdir(exist_ok=True)
                    library.get_video_info(settings, identifier)
                    self.assertFalse(base.exists())
                    library.get_video_info(settings, identifier)
                    self.assertTrue(staging.is_dir())
                    stream = staging / '0.raw.part'
                    stream.write_bytes(b'partial')
                    self.assertEqual(stream.read_bytes(), b'partial')

    def test_deleted_root_record_does_not_remove_download_root(self) -> None:
        """验证删除下载根目录中的旧记录时保留下载根目录本身；无返回值。"""
        old_path = self.root / 'old_record.json'
        old_path.write_bytes(b'{}')

        def commit() -> None:
            """仅由外层事务删除旧记录，本回调不写入文件；无参数及返回值。"""

        apply_changes(self.root, [], [], commit, [old_path])
        self.assertFalse(old_path.exists())
        self.assertTrue(self.root.is_dir())

    def test_collection_keeps_each_bv_in_each_quality_directory(self) -> None:
        """验证合集三个目录中各有两份 BV 索引，同一 BV 跨画质读取不被误判重复；无返回值。"""
        first = self.fixture(shared=True)
        second = self.fixture('BV1yy411c7mD', shared=True)
        first.migrate_directory_indexes()
        second.migrate_directory_indexes()
        self.assertEqual(len(list(self.root.rglob('*_video_info.json'))), 6)
        self.assertEqual(list(self.root.rglob('video_info.json')), [])
        for bvid in (self.bvid, 'BV1yy411c7mD'):
            loaded = metadata.open_video(self.root, bvid)
            self.assertEqual(len(loaded.data['files']), 3)
            self.assertEqual(len(loaded.index_paths), 3)

    def test_split_failure_restores_all_old_json_and_removes_new_json(self) -> None:
        """验证第二份 JSON 写入失败时旧总索引完整恢复，新建索引被清理，媒体不变；无返回值。"""
        record = self.fixture()
        old_path, original = record.path, record.path.read_bytes()
        write = metadata.atomic_text
        calls = []

        def fail_second(path: Path, content: str) -> None:
            """将 content 写到 path，仅对第二个目录索引模拟磁盘错误；无返回值。"""
            calls.append(path)
            if len(calls) == 2:
                raise OSError('disk full')
            write(path, content)

        with patch.object(metadata, 'atomic_text', side_effect=fail_second), self.assertRaises(OSError):
            record.migrate_directory_indexes()
        self.assertEqual(old_path.read_bytes(), original)
        self.assertEqual(list(self.root.rglob('video_info.json')), [old_path])
        self.assertEqual(record.data['schema_version'], 4)
        self.assertTrue(all(record.resolve_file(item).is_file() for item in record.data['files']))

    def test_copied_single_directory_reads_without_other_qualities(self) -> None:
        """验证单独复制一个画质目录后独立读取，缺失其他 P 时只报告缺失而不联网补数据；无返回值。"""
        record = self.fixture(legacy=False)
        path = next(path for path in record.index_paths if 'qn125_' in path.parent.name)
        with tempfile.TemporaryDirectory() as root:
            copied_root = Path(root).resolve()
            copied = copied_root / path.parent.name
            shutil.copytree(path.parent, copied)
            loaded = library.get_video_info(replace(self.settings, output_dir=copied_root), self.bvid)
            self.assertEqual([item['part'] for item in loaded.data['files']], [2])
            expected_folder = (copied_root / f'投稿_{self.bvid}_分P') if self.settings.mix_quality else copied
            self.assertEqual(loaded.resolve_file(loaded.data['files'][0]).parent, expected_folder)
            self.assertIs(loaded.mix_quality, self.settings.mix_quality)
            with patch.object(auth, 'authenticated_session', side_effect=AssertionError('不应联网')):
                with self.assertRaisesRegex(ToolError, '尚未下载'):
                    merge.merge_parts(replace(self.settings, output_dir=copied_root), 'unused', loaded, [1, 2], quality=self.qualities[1])

    def test_duplicate_groups_and_duplicate_entries_report_conflicts(self) -> None:
        """验证不同目录组的同 BV 以及同组重复条目均明确报错；无返回值。"""
        record = self.fixture(legacy=False)
        path = record.index_paths[0]
        group = self.root / 'another_group'
        group.mkdir()
        copied = group / path.parent.name
        shutil.copytree(path.parent, copied)
        with self.assertRaisesRegex(ToolError, '多个投稿目录'):
            metadata.open_video(self.root, self.bvid)

    def test_v5_rejects_cross_directory_media_paths(self) -> None:
        """验证 v5 磁盘索引不接受指向其他目录的媒体路径；无返回值。"""
        record = self.fixture(legacy=False)
        path = record.index_paths[0]
        data = metadata.read_json(path)
        data['files'][0]['path'] = '../elsewhere/video.mp4'
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ToolError, '跨目录'):
            metadata.open_video(self.root, self.bvid)

    def test_download_commit_failure_restores_media_and_all_directory_indexes(self) -> None:
        """验证换片提交中途写第二份索引失败时，全部目录 JSON 和旧媒体同时恢复；无返回值。"""
        self.fixture(legacy=False)
        record = metadata.open_video(self.root, self.bvid)
        record.data['current_parts'][1]['cid'] = 202
        record.save()
        originals = {path: path.read_bytes() for path in record.index_paths}
        item = next(item for item in record.data['files'] if item['part'] == 2)
        media_path = record.resolve_file(item)
        old_media = media_path.read_bytes()
        downloader = MagicMock()

        def download(filename: str, info: dict) -> tuple[bool, bool]:
            """将 info 的新版本内容写到 filename，返回成功状态。"""
            Path(filename).write_bytes(b'new-source-content')
            return True, True

        def mux(executable: str, sources: list[Path], formats: list[dict], folder: Path, stem: str) -> Path:
            """模拟 executable 将 sources/formats 封装到 folder/stem，返回完整新媒体路径。"""
            output = folder / (stem + '.mp4')
            output.write_bytes(sources[0].read_bytes())
            return output

        write, writes = metadata.atomic_text, []

        def fail_second(path: Path, content: str) -> None:
            """保存 content 到 path，在第二份目录索引处模拟失败；无返回值。"""
            writes.append(path)
            if len(writes) == 2:
                raise OSError('disk full')
            write(path, content)

        info = {'id': f'{self.bvid}_p2', 'title': '片段2', 'selected_quality': {'code': 125, 'name': 'HDR 真彩'},
                'requested_formats': [{'format_id': '30125', 'vcodec': 'hevc', 'acodec': 'aac',
                                       'width': 3840, 'height': 2160, 'url': 'https://example.com/video', 'ext': 'mp4'}]}
        downloader.dl.side_effect = download
        with patch.object(metadata, 'atomic_text', side_effect=fail_second), patch.object(transfer.media, 'mux_streams', side_effect=mux):
            with self.assertRaises(OSError):
                transfer.save_video(downloader, info, self.settings, 'unused', '投稿', 3, source_cid=202, prepared=True)
        self.assertEqual(media_path.read_bytes(), old_media)
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_same_group_duplicate_quality_and_parts_are_rejected(self) -> None:
        """验证同一目录组复制出重复画质及分 P 索引时不能静默重复加载；无返回值。"""
        record = self.fixture(legacy=False)
        path = record.index_paths[0]
        duplicate = path.parent.with_name(path.parent.name.replace('_分P', '_duplicate_分P'))
        shutil.copytree(path.parent, duplicate)
        with self.assertRaisesRegex(ToolError, '多个目录|重复'):
            metadata.open_video(self.root, self.bvid)
