"""离线验证内容指纹、远端换片、来源快照及现有文件基线，不访问真实下载目录。"""

from copy import deepcopy
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bilibili_downloader import config, errors, library, media, metadata, models, provenance, runtime
from bilibili_downloader import downloader as transfer
from bilibili_downloader import merge
from bilibili_downloader.merged_records import MergedVideoInfo
from tests import CONFIG_PATH


class ContentUpdateTests(unittest.TestCase):
    """使用真实临时文件和索引，仅模拟网络与媒体工具，验证来源变化的完整生命周期。"""

    def setUp(self) -> None:
        """准备隔离目录、CID 列表和媒体模拟；无参数及返回值。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = replace(config.load_settings(CONFIG_PATH), output_dir=self.root, overwrite_merged=False)
        self.bvid, self.title = 'BV1xx411c7mD', '内容更新测试'
        self.quality = models.Quality(80, '1080P')
        self.fmt = {'format_id': '80', 'vcodec': 'avc1', 'acodec': 'none', 'width': 64, 'height': 64, 'fps': 5,
                    'url': 'https://example.com/video', 'ext': 'mp4'}
        self.payload = b'initial-source'
        self.downloads = []
        self.downloader = MagicMock()
        self.downloader.dl.side_effect = self.download_stream
        self.enterContext(patch.object(media, 'mux_streams', side_effect=self.mux))
        self.enterContext(patch.object(metadata, 'probe_media', return_value={'duration_seconds': 1, 'errors': []}))
        self.concatenate = self.enterContext(patch.object(runtime, 'run_process', side_effect=self.concatenate_media))
        self.set_pages([101, 102])

    def record(self) -> metadata.VideoInfo:
        """读取当前测试投稿的真实源索引，返回 VideoInfo；无参数。"""
        return library.get_video_info(self.settings, self.bvid)

    def set_pages(self, cids: list[int]) -> None:
        """将正整数列表 cids 作为最新原站分 P 列表写入索引；无返回值。"""
        record = library.get_video_info(self.settings, self.bvid, self.title, len(cids))
        library.update_source_info(record, {'bvid': self.bvid, 'title': self.title, 'owner': {'mid': 1, 'name': 'test'},
                                           'pages': [{'page': part, 'cid': cid} for part, cid in enumerate(cids, 1)]})
        record.save()

    def download_stream(self, filename: str, info: dict) -> tuple[bool, bool]:
        """将当前 payload 写入 info 对应的 filename 并记录实际传输，返回成功标志。"""
        self.downloads.append(Path(filename))
        Path(filename).write_bytes(self.payload)
        return True, True

    def mux(self, executable: str, sources: list[Path], formats: list[dict], folder: Path, stem: str) -> Path:
        """模拟 executable 将 sources/formats 封装至 folder/stem，返回新文件路径。"""
        output = folder / f'{stem}.mp4'
        output.write_bytes(sources[0].read_bytes())
        return output

    def concatenate_media(self, command: list[str], **options: object) -> subprocess.CompletedProcess:
        """按 command 写入非空合并媒体；options 为进程参数，返回成功结果。"""
        Path(command[-1]).write_bytes(b'merged-content')
        return subprocess.CompletedProcess(command, 0, '', '')

    def download(self, part: int, cid: int) -> None:
        """下载正整数 part 对应的 cid 版本，走真实复用与文件事务；无返回值。"""
        info = {'id': f'{self.bvid}_p{part}', 'title': f'片段{part}',
                'selected_quality': {'code': 80, 'name': '1080P'}, 'requested_formats': [self.fmt]}
        transfer.save_video(self.downloader, info, self.settings, 'ffmpeg', self.title,
                            self.record().data['total_parts'], source_cid=cid)

    def prepare_merge(self) -> Path:
        """下载最初两个分 P 并合并，返回成品路径；无参数。"""
        self.download(1, 101)
        self.download(2, 102)
        return merge.merge_parts(self.settings, 'ffmpeg', self.record(), quality=self.quality)

    def index(self) -> MergedVideoInfo:
        """返回当前测试投稿的合并索引；无参数。"""
        return MergedVideoInfo(self.root, self.record().quality_directory(self.quality, role='merged'))

    def test_same_cid_reuses_and_changed_cid_downloads_new_version(self) -> None:
        """验证同格式同 CID 复用，CID 改变重新下载并更新指纹，旧版本断点不混用；无返回值。"""
        self.download(1, 101)
        before = self.record().data['files'][0]
        self.download(1, 101)
        self.assertEqual(len(self.downloads), 1)
        old_staging = self.downloads[0].parent
        old_staging.mkdir(parents=True, exist_ok=True)
        (old_staging / '0.80.mp4.part').write_bytes(b'old-version-partial')
        self.set_pages([201, 102])
        self.payload = b'replaced-source'
        self.download(1, 201)
        item = self.record().data['files'][0]
        self.assertEqual(len(self.downloads), 2)
        self.assertNotEqual(self.downloads[0].parent, self.downloads[1].parent)
        self.assertTrue((old_staging / '0.80.mp4.part').is_file())
        self.assertEqual(item['source_cid'], 201)
        self.assertNotEqual(before['content_fingerprint']['sha256'], item['content_fingerprint']['sha256'])

    def test_append_and_delete_rebuild_full_merge_even_when_overwrite_disabled(self) -> None:
        """验证追加和删减分 P 均更新同名全量合并，历史媒体保留且索引条目不重复；无返回值。"""
        destination = self.prepare_merge()
        self.set_pages([101, 102, 103])
        self.download(3, 103)
        merge.merge_parts(self.settings, 'ffmpeg', self.record(), quality=self.quality)
        self.assertEqual(self.index().data['files'][0]['source_parts'], [1, 2, 3])
        self.set_pages([101, 102])
        self.assertEqual(merge.merge_parts(self.settings, 'ffmpeg', self.record(), quality=self.quality), destination)
        self.assertEqual(self.concatenate.call_count, 3)
        self.assertEqual(self.index().data['files'][0]['source_parts'], [1, 2])
        self.assertEqual(len(self.index().data['files']), 1)
        self.assertTrue(self.record().resolve_file(self.record().data['files'][2]).is_file())

    def test_fixed_group_ignores_updates_outside_group(self) -> None:
        """验证固定分组不因组外新增而更新，组内换片则重新合并；无返回值。"""
        self.prepare_merge()
        merge.merge_parts(self.settings, 'ffmpeg', self.record(), [2, 1, 2], quality=self.quality)
        self.set_pages([101, 102, 103])
        self.download(3, 103)
        merge.merge_parts(self.settings, 'ffmpeg', self.record(), [2, 1, 2], quality=self.quality)
        self.assertEqual(self.concatenate.call_count, 2)
        self.set_pages([101, 202, 103])
        self.download(2, 202)
        merge.merge_parts(self.settings, 'ffmpeg', self.record(), [2, 1, 2], quality=self.quality)
        snapshot = self.index().data['files'][1]['source_snapshot']
        self.assertEqual([item['source_cid'] for item in snapshot], [202, 101, 202])
        self.assertEqual(self.concatenate.call_count, 3)

    def test_local_content_change_rebuilds_but_timestamp_only_change_reuses(self) -> None:
        """验证本地同大小内容改写触发合并，只有修改时间变化而字节不变时复用；无返回值。"""
        self.prepare_merge()
        record = self.record()
        source = record.resolve_file(record.data['files'][0])
        stamp = source.stat()
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000000))
        merge.merge_parts(self.settings, 'ffmpeg', self.record(), quality=self.quality)
        self.assertEqual(self.concatenate.call_count, 1)
        source.write_bytes(b'changed-source')
        stamp = source.stat()
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000000))
        merge.merge_parts(self.settings, 'ffmpeg', self.record(), quality=self.quality)
        self.assertEqual(self.concatenate.call_count, 2)

    def test_download_failure_preserves_old_version_and_blocks_stale_merge(self) -> None:
        """验证换片下载失败保留旧媒体与 CID，组内版本不一致时拒绝合并；无返回值。"""
        destination = self.prepare_merge()
        before = deepcopy(self.index().data['files'])
        record = self.record()
        source = record.resolve_file(record.data['files'][0])
        self.set_pages([201, 102])
        self.downloader.dl.return_value = (False, False)
        self.downloader.dl.side_effect = None
        with self.assertRaises(errors.ToolError):
            self.download(1, 201)
        self.assertEqual(source.read_bytes(), b'initial-source')
        self.assertEqual(self.record().data['files'][0]['source_cid'], 101)
        with self.assertRaisesRegex(errors.ToolError, 'CID'):
            merge.merge_parts(self.settings, 'ffmpeg', self.record(), [1, 2], quality=self.quality)
        self.assertEqual(self.index().data['files'], before)
        self.assertEqual(MergedVideoInfo.for_results(self.record()).data['merge_results'][-1]['status'], 'failed')
        self.assertEqual(destination.read_bytes(), b'merged-content')

    def test_source_commit_failure_restores_old_file_and_version(self) -> None:
        """验证新 CID 的完成记录保存失败时回滚源文件、指纹和版本；无返回值。"""
        self.download(1, 101)
        self.set_pages([201, 102])
        original = metadata.VideoInfo.save

        def fail_new_version(record: metadata.VideoInfo) -> None:
            """保存 record，但对新 CID 的成功提交模拟磁盘错误；无返回值。"""
            if any(item.get('source_cid') == 201 for item in record.data['files']):
                raise OSError('disk full')
            original(record)

        before = self.record().path.read_bytes()
        self.payload = b'new-version'
        with patch.object(metadata.VideoInfo, 'save', new=fail_new_version), self.assertRaises(OSError):
            self.download(1, 201)
        record = self.record()
        self.assertEqual(record.path.read_bytes(), before)
        self.assertEqual(record.resolve_file(record.data['files'][0]).read_bytes(), b'initial-source')

    def test_adopt_existing_files_adds_baseline_without_download_or_merge(self) -> None:
        """验证显式采用现有文件只补信息，来源按旧合并实际顺序登记，之后不重复下载或合并；无返回值。"""
        destination = self.prepare_merge()
        record = self.record()
        for item in record.data['files']:
            item.pop('source_cid')
            item.pop('content_fingerprint')
        record.save()
        index = self.index()
        index.data['files'][0].pop('source_snapshot')
        index.save()
        original = destination.stat()
        self.concatenate.reset_mock()
        self.downloader.dl.reset_mock()
        self.assertEqual(library.adopt_existing_content(self.record()), (2, 1, []))
        self.download(1, 101)
        self.download(2, 102)
        merge.merge_parts(self.settings, 'ffmpeg', self.record(), quality=self.quality)
        self.downloader.dl.assert_not_called()
        self.concatenate.assert_not_called()
        self.assertEqual(destination.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertEqual([item['source_cid'] for item in self.index().data['files'][0]['source_snapshot']], [101, 102])

    def test_unknown_merge_snapshot_is_rebuilt_once(self) -> None:
        """验证未显式建立基线的旧合并不能仅凭存在而复用，重建后可正常复用；无返回值。"""
        self.prepare_merge()
        index = self.index()
        index.data['files'][0].pop('source_snapshot')
        index.save()
        for _ in range(2):
            merge.merge_parts(self.settings, 'ffmpeg', self.record(), quality=self.quality)
        self.assertEqual(self.concatenate.call_count, 2)

    def test_title_and_directory_change_preserve_reuse_and_fingerprints(self) -> None:
        """验证投稿改名、同级目录整体迁入分组及媒体信息刷新均保留来源快照，不重合并；无返回值。"""
        destination = self.prepare_merge()
        snapshot = self.index().data['files'][0]['source_snapshot']
        group = self.root / 'group'
        group.mkdir()
        for folder in list(self.root.glob(self.title + '*')):
            folder.rename(group / folder.name)
        record = self.record()
        record.data['title'] = '新的投稿标题'
        record.save()
        record.refresh_local('ffmpeg')
        actual = merge.merge_parts(self.settings, 'ffmpeg', self.record(), quality=self.quality)
        self.assertEqual(actual, group / destination.parent.name / destination.name)
        self.assertEqual(self.index().data['files'][0]['source_snapshot'], snapshot)
        self.assertTrue(all(item.get('content_fingerprint') for item in self.record().data['files']))
        self.assertEqual(self.concatenate.call_count, 1)

    def test_cancel_during_updated_download_keeps_previous_success(self) -> None:
        """验证新版本在封装后计算指纹时取消，旧文件与旧 CID 均不被替换；无返回值。"""
        self.download(1, 101)
        self.set_pages([201, 102])
        before = self.record().path.read_bytes()
        self.payload = b'new-version'
        with patch.object(provenance, 'file_fingerprint', side_effect=errors.DownloadCancelled('取消')):
            with self.assertRaises(errors.DownloadCancelled):
                self.download(1, 201)
        record = self.record()
        self.assertEqual(record.path.read_bytes(), before)
        self.assertEqual(record.resolve_file(record.data['files'][0]).read_bytes(), b'initial-source')

    def test_changed_sources_merge_failure_preserves_old_result_and_snapshot(self) -> None:
        """验证关闭覆盖时因来源变化触发的合并遇到失败或取消，仍保留旧成品及来源快照；无返回值。"""
        destination = self.prepare_merge()
        self.set_pages([201, 102])
        self.payload = b'new-version'
        self.download(1, 201)
        before = deepcopy(self.index().data['files'])
        failures = [subprocess.CompletedProcess([], 1, '', '模拟失败'), OSError('进程失败'), errors.DownloadCancelled('取消')]
        for failure in failures:
            with self.subTest(failure=failure):
                if isinstance(failure, BaseException):
                    self.concatenate.side_effect = failure
                else:
                    self.concatenate.side_effect = None
                    self.concatenate.return_value = failure
                with self.assertRaises((errors.ToolError, OSError, errors.DownloadCancelled)):
                    merge.merge_parts(self.settings, 'ffmpeg', self.record(), quality=self.quality)
                self.assertEqual(self.index().data['files'], before)
                result = MergedVideoInfo.for_results(self.record()).data['merge_results'][-1]
                self.assertEqual(result['status'], 'interrupted' if isinstance(failure, KeyboardInterrupt) else 'failed')
                self.assertEqual(destination.read_bytes(), b'merged-content')


class FingerprintTests(unittest.TestCase):
    """验证缓存命中和不可用源文件的指纹行为。"""

    def test_cached_fingerprint_avoids_reopening_unchanged_file(self) -> None:
        """验证有效缓存直接返回摘要，源文件未变化时不再次读取内容；无返回值。"""
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'video.mp4'
            path.write_bytes(b'content')
            fingerprint = provenance.file_fingerprint(path)
            self.assertEqual(fingerprint['sha256'], hashlib.sha256(b'content').hexdigest())
            with patch.object(Path, 'open', side_effect=AssertionError('不应重读')):
                self.assertEqual(provenance.file_fingerprint(path, fingerprint), fingerprint)
