"""离线验证清晰度确认、多版本归档、旧文件迁移与下载前重新确认；不读取真实账号。"""

from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from yt_dlp import YoutubeDL

from bilibili_downloader import api, application, auth, errors, library, media, metadata, models, quality, workflow
from bilibili_downloader import downloader as download_service
from bilibili_downloader import merge as merge_service
from bilibili_downloader.merged_records import MergedVideoInfo
from bilibili_downloader import subtitles as subtitle_service
from bilibili_downloader import config as configuration
import download_bilibili as cli_entry
from bilibili_downloader import runtime
from tests import CONFIG_PATH, fake_auth_snapshot


def sample_info(part: int = 1, codes: tuple[int, ...] = (80, 120)) -> dict:
    """创建 part 对应的离线解析数据；codes 为平台档位编号元组，返回含多编码与音频的格式字典。"""
    names = {80: '1080P 高清', 120: '4K 超清', 116: '1080P 60帧', 125: 'HDR 真彩'}
    formats = [{'format_id': 'audio', 'url': 'https://example.com/audio', 'ext': 'm4a',
                'vcodec': 'none', 'acodec': 'mp4a', 'tbr': 192}]
    for code in codes:
        for codec in ('avc1', 'hev1'):
            formats.append({'format_id': f'{code}-{codec}', 'url': f'https://example.com/{code}-{codec}',
                            'quality': code, 'format': names[code], 'ext': 'mp4', 'vcodec': codec,
                            'acodec': 'none', 'width': 3840 if code == 120 else 1920,
                            'height': 2160 if code == 120 else 1080, 'fps': 60 if code == 116 else 30})
    return {'id': f'BV1xx411c7mD_p{part}', 'title': f'片段{part}', 'formats': formats}


class VideoQualityTests(unittest.TestCase):
    """使用真实 yt-dlp 选流与临时目录，验证交互选择始终约束最终传输格式。"""

    def setUp(self) -> None:
        """创建临时配置、静默输出和真实离线选流器；无参数及返回值。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = replace(configuration.load_settings(CONFIG_PATH), output_dir=self.root, threads=1,
                                mix_quality=False)
        self.enterContext(patch.object(auth, 'snapshot_session', return_value=fake_auth_snapshot()))
        self.downloader = YoutubeDL({'quiet': True, 'no_warnings': True, 'format': self.settings.format_selector,
                                     'format_sort': self.settings.format_sort})
        self.addCleanup(self.downloader.close)
        self.out = StringIO()
        self.enterContext(redirect_stdout(self.out))
        self.enterContext(redirect_stderr(self.out))

    def test_catalog_groups_codecs_and_excludes_unusable_formats(self) -> None:
        """验证同档编码合并，音频/DRM/无地址和仅提示的会员档位不能成为可选项；无返回值。"""
        info = sample_info()
        info['formats'].extend([{**info['formats'][1], 'quality': 125, 'has_drm': True},
                                {**info['formats'][1], 'quality': 116, 'url': None}])
        info['support_formats'] = [{'quality': 127, 'new_description': '8K 会员'}]
        catalog = quality.available_qualities(self.downloader, info)
        self.assertEqual(set(catalog), {80, 120})
        self.assertEqual(catalog[80].name, '1080P 高清')

    def test_confirmed_lower_quality_overrides_automatic_highest(self) -> None:
        """确认 1080P 后即使有 4K 及旧 requested_formats 也不能选择其他档位，音频仍自动选择；无返回值。"""
        raw = sample_info()
        raw['requested_formats'] = [raw['formats'][-1], raw['formats'][0]]
        selected = quality.select_quality(self.downloader, raw, models.Quality(80, '1080P 高清'))
        self.assertEqual([fmt['quality'] for fmt in selected['requested_formats'] if fmt['vcodec'] != 'none'], [80])
        self.assertEqual(selected['requested_formats'][1]['vcodec'], 'none')
        self.assertEqual(len(raw['formats']), 5)
        with self.assertRaises(quality.QualityUnavailable):
            quality.select_quality(self.downloader, raw, models.Quality(125, 'HDR'))

    def test_one_ceiling_selects_highest_available_for_each_part(self) -> None:
        """验证只确认一次上限，各P选择不超过它的最高实际档位，普通1080P与60帧保持独立；无返回值。"""
        q80, q120, q116 = (models.Quality(code, name) for code, name in ((80, '1080P'), (120, '4K'), (116, '1080P60')))
        with patch('builtins.input', side_effect=['1', '2']) as prompt:
            assignments, skipped = quality.confirm_qualities({1: {120: q120, 80: q80}, 2: {116: q116, 80: q80}, 3: {80: q80}})
        self.assertEqual({part: item.code for part, item in assignments.items()}, {1: 120, 2: 116, 3: 80})
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(skipped, [])

    def test_invalid_empty_skip_cancel_and_eof(self) -> None:
        """验证回车/非法输入不默认选最高，跳过返回未完成项，q 和 EOF 中止；无返回值。"""
        catalog = {1: {80: models.Quality(80, '1080P')}, 2: {}}
        with patch('builtins.input', side_effect=['', 'x', '0', 's']):
            assignments, skipped = quality.confirm_qualities(catalog)
        self.assertEqual(assignments, {})
        self.assertEqual(skipped, [1, 2])
        for response in ('q', EOFError()):
            with self.subTest(response=response), patch('builtins.input', side_effect=[response]), self.assertRaises(KeyboardInterrupt):
                quality.confirm_qualities(catalog)

    def test_series_rank_menu_validates_input_and_keeps_episode_identity(self) -> None:
        """验证跨季同号P独立计数，空值和非法输入不默认选档，第二档返回排名而非画质编号；无返回值。"""
        choices = {code: models.Quality(code, f'画质{code}') for code in (64, 80, 120)}
        catalogs = {('ss1', 1): {80: choices[80], 120: choices[120]}, ('ss2', 1): {64: choices[64]}}
        with patch('builtins.input', side_effect=['', 'x', '0', '2']) as prompt:
            self.assertEqual(quality.confirm_rank(catalogs), 2)
        self.assertEqual(prompt.call_count, 4)
        self.assertIn('ss1 P1、ss2 P1', self.out.getvalue())
        self.assertIn('本档支持 1/2 集', self.out.getvalue())

    def record(self) -> metadata.VideoInfo:
        """创建可离线使用的两分 P 投稿记录并清空来源错误；返回 VideoInfo。"""
        record = metadata.open_video(self.root, 'BV1xx411c7mD', '测试投稿', 2)
        record.data['errors'] = []
        record.save()
        return record

    def test_multiple_qualities_coexist_and_repeat_skips(self) -> None:
        """验证同一 P 的两种清晰度独立保存和续传，重复同格式跳过且字幕留在对应目录；无返回值。"""
        self.record()
        def transfer(filename: str, info: dict) -> tuple[bool, bool]:
            """模拟 info 的传输结果写入 filename，返回成功状态。"""
            Path(filename).write_bytes(b'stream')
            return True, True

        def mux(executable: str, sources: list[Path], formats: list[dict], folder: Path, stem: str) -> Path:
            """模拟 executable 将 sources/formats 封装到 folder/stem，返回媒体路径。"""
            destination = folder / f'{stem}.mp4'
            destination.write_bytes(b'video')
            return destination

        with patch.object(self.downloader, 'dl', side_effect=transfer) as download, patch.object(media, 'mux_streams', side_effect=mux), patch.object(metadata, 'probe_media', return_value={'errors': []}):
            for code in (80, 120, 80):
                selected = quality.select_quality(self.downloader, sample_info(), models.Quality(code, f'画质{code}'))
                folder, _ = download_service.save_video(self.downloader, selected, self.settings, 'ffmpeg', '测试投稿', 2, source_cid=1)
                self.assertEqual(folder, self.root.resolve() / f'测试投稿_BV1xx411c7mD_qn{code}_画质{code}_分P')
            self.assertEqual(download.call_count, 4)
        record = library.get_video_info(self.settings, 'BV1xx411c7mD')
        self.assertEqual(len(record.data['files']), 2)
        self.assertEqual({item['quality_id'] for item in record.data['files']}, {80, 120})
        self.assertTrue(all(record.resolve_file(item).is_file() for item in record.data['files']))

    def test_legacy_migration_preserves_files_subtitles_and_streams(self) -> None:
        """验证已知画质按后缀归档，未知画质不由尺寸猜测，旧字幕/断点流同步移动且 v3 可重复读取；无返回值。"""
        record = self.record()
        record.data['schema_version'] = 1
        for part, known in ((1, True), (2, False)):
            path = record.path.parent / f'片段{part}_BV1xx411c7mD_p{part}.mp4'
            path.write_bytes(b'old')
            path.with_suffix('.zh.srt').write_text('字幕', encoding='utf-8')
            item = record.entry(f'BV1xx411c7mD_p{part}', part=part)
            item.pop('quality_dir')
            item.update(formats=[{'quality': 80, 'format': '1080P'}] if known else [{'width': 1920, 'height': 1080}])
            record.record_file(item, path)
            stream = record.path.parent / '.streams' / f'BV1xx411c7mD_p{part}'
            stream.mkdir(parents=True)
            (stream / '0.raw.part').write_bytes(b'partial')
        record.save()
        migrated = library.get_video_info(self.settings, 'BV1xx411c7mD')
        self.assertEqual(migrated.data['schema_version'], 8)
        self.assertEqual({item['quality_dir'] for item in migrated.data['files']}, {'qn80_1080P', '旧版_未确认清晰度'})
        for item in migrated.data['files']:
            media = migrated.resolve_file(item)
            self.assertTrue(media.with_suffix('.zh.srt').is_file())
            self.assertEqual((media.parent / '.streams' / f"BV1xx411c7mD_p{item['part']}" / '0.raw.part').read_bytes(), b'partial')
        # 保存/聚合时允许刷新顶层更新时间；重复读取应保持全部业务信息，不依赖两次操作落在同一秒。
        reloaded = library.get_video_info(self.settings, 'BV1xx411c7mD').data
        self.assertEqual({key: value for key, value in reloaded.items() if key != 'updated_at'},
                         {key: value for key, value in migrated.data.items() if key != 'updated_at'})

    def test_migration_conflict_and_save_failure_roll_back(self) -> None:
        """验证清晰度迁移的目标冲突和记录保存失败保留原媒体、记录及字幕；无返回值。"""
        record = self.record()
        record.data['schema_version'] = 4
        source = record.path.parent / 'old.mp4'
        source.write_bytes(b'original')
        item = record.entry('BV1xx411c7mD_p1', part=1)
        record.record_file(item, source)
        record.save()
        before = record.path.read_bytes()
        with patch.object(record, 'save', side_effect=OSError('模拟保存失败')), self.assertRaises(OSError):
            record.organize_qualities()
        self.assertEqual(source.read_bytes(), b'original')
        self.assertEqual(record.path.read_bytes(), before)
        destination = record.quality_directory(models.LEGACY_QUALITY) / source.name
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(b'other')
        with self.assertRaises(errors.ToolError):
            record.organize_qualities()
        self.assertEqual(source.read_bytes(), b'original')
        self.assertEqual(destination.read_bytes(), b'other')

    def test_changed_quality_stays_under_original_ceiling_without_reprompt(self) -> None:
        """验证派发前档位消失时在原上限内自动降低，不重复提示，旧待处理条目标记替换原因；无返回值。"""
        self.record()
        target = models.VideoTarget('https://www.bilibili.com/video/BV1xx411c7mD?p=1', 1, 1, 2)
        source = {'bvid': 'BV1xx411c7mD', 'title': '测试投稿', 'owner': {'mid': 1, 'name': 'test'}}
        args = cli_entry.parse_args(['--url', target.url])
        with patch.object(download_service, 'make_downloader', return_value=self.downloader), patch.object(auth, 'ensure_login'), patch.object(self.downloader, 'extract_info', side_effect=[sample_info(), sample_info(codes=(80,)), sample_info(codes=(80,))]) as extract, patch('builtins.input', side_effect=['1', '1']) as prompt, patch.object(download_service, 'prepare_video', return_value=MagicMock(existing=None, queued_bytes=lambda: 0)) as save, patch.object(download_service, 'transfer_video', side_effect=lambda downloader, video, settings, **kwargs: video), patch.object(download_service, 'finish_video', return_value=(self.root, 'test')), patch.object(subtitle_service, 'list_subtitles', return_value=[]), patch.object(workflow, 'print_video_info'):
            failures = workflow.download_submission(MagicMock(), self.settings, 'ffmpeg', source, [target], args)
        self.assertEqual(failures, [])
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(extract.call_count, 2)
        self.assertEqual(save.call_args.args[0]['selected_quality']['code'], 80)
        record = library.get_video_info(self.settings, source['bvid'])
        self.assertEqual(next(item['status'] for item in record.data['files'] if item['quality_id'] == 120), 'failed')

    def test_duplicate_bv_links_share_one_ceiling(self) -> None:
        """验证同一BV多链接合并查询且只确认一次上限，各P自动分配实际档位；无返回值。"""
        targets = [models.VideoTarget(f'https://www.bilibili.com/video/BV1xx411c7mD?p={part}', part, part, 2) for part in (1, 2)]
        source = {'bvid': 'BV1xx411c7mD', 'title': '测试投稿', 'owner': {'mid': 1, 'name': 'test'}}
        session = MagicMock()
        args = cli_entry.parse_args(['--url', targets[0].url, '--url', targets[1].url, '--url', targets[0].url])
        infos = [sample_info(), sample_info(2, (80,)), sample_info(), sample_info(2, (80,))]
        with patch.object(configuration, 'load_settings', return_value=self.settings), patch.object(media, 'resolve_ffmpeg', return_value='ffmpeg'), patch.object(auth, 'authenticated_session', return_value=session), patch.object(auth, 'ensure_login', return_value={}), patch.object(api, 'resolve_targets', side_effect=[[targets[0]], [targets[1]], [targets[0]]]), patch.object(api, 'api_json', return_value={'data': source}), patch.object(download_service, 'make_downloader', return_value=self.downloader), patch.object(self.downloader, 'extract_info', side_effect=infos) as extract, patch.object(download_service, 'prepare_video', return_value=MagicMock(existing=None, queued_bytes=lambda: 0)) as save, patch.object(download_service, 'transfer_video', side_effect=lambda downloader, video, settings, **kwargs: video), patch.object(download_service, 'finish_video', return_value=(self.root, 'test')), patch.object(subtitle_service, 'list_subtitles', return_value=[]), patch.object(workflow, 'print_video_info'), patch('builtins.input', side_effect=['1', '1']) as prompt:
            self.assertEqual(application.run(args), 0)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(extract.call_count, 4)
        self.assertEqual([call.args[0]['selected_quality']['code'] for call in save.call_args_list], [120, 80])

    def test_cancel_and_skip_never_start_transfer(self) -> None:
        """验证下载确认阶段取消返回 130、跳过返回 2，均不调用视频或字幕传输；无返回值。"""
        target = models.VideoTarget('https://www.bilibili.com/video/BV1xx411c7mD?p=1', 1, 1, 1)
        source = {'bvid': 'BV1xx411c7mD', 'title': '测试投稿', 'owner': {'mid': 1, 'name': 'test'}}
        for answer, expected in (('q', 130), ('s', 2)):
            with self.subTest(answer=answer), patch.object(configuration, 'load_settings', return_value=self.settings), patch.object(media, 'resolve_ffmpeg', return_value='ffmpeg'), patch.object(auth, 'authenticated_session', return_value=MagicMock()), patch.object(auth, 'ensure_login', return_value={}), patch.object(api, 'resolve_targets', return_value=[target]), patch.object(api, 'api_json', return_value={'data': source}), patch.object(download_service, 'make_downloader', return_value=self.downloader), patch.object(self.downloader, 'extract_info', return_value=sample_info()), patch.object(download_service, 'transfer_video') as save, patch.object(subtitle_service, 'fetch_subtitles') as subtitles, patch('builtins.input', return_value=answer):
                self.assertEqual(cli_entry.main(['--url', target.url]), expected)
                save.assert_not_called()
                subtitles.assert_not_called()

    def test_merge_filters_one_quality_and_rejects_missing_parts(self) -> None:
        """验证多个清晰度共存时拼接只选一个目录，其他目录不能补齐所选清晰度的缺失 P；无返回值。"""
        record = self.record()
        for part, code in ((1, 80), (2, 80), (1, 120)):
            q = models.Quality(code, f'画质{code}')
            folder = record.quality_directory(q)
            folder.mkdir(exist_ok=True)
            path = folder / f'片段{part}_BV1xx411c7mD_p{part}.mp4'
            path.write_bytes(b'video')
            item = record.entry(f'BV1xx411c7mD_p{part}', part=part, quality=q)
            item['formats'] = [{'vcodec': 'avc1', 'acodec': 'none', 'width': code, 'height': code}]
            record.record_file(item, path)
        record.save()
        with patch('builtins.input', return_value='1'), patch.object(runtime, 'run_process') as ffmpeg, self.assertRaises(errors.ToolError):
            merge_service.merge_parts(self.settings, 'ffmpeg', record)
        ffmpeg.assert_not_called()
        with patch('builtins.input', return_value='2'):
            self.assertEqual(quality.choose_merge_quality(record).code, 80)
        def concatenate(command: list[str], **options: object) -> subprocess.CompletedProcess:
            """按 command 生成模拟拼接文件；options 为进程选项，返回成功结果。"""
            Path(command[-1]).write_bytes(b'merged')
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch('builtins.input', return_value='2'), patch.object(runtime, 'run_process', side_effect=concatenate), patch.object(metadata, 'probe_media', return_value={'errors': []}):
            result = merge_service.merge_parts(self.settings, 'ffmpeg', record)
        self.assertEqual(result.parent, record.quality_directory(models.Quality(80, '画质80'), role='merged'))
        merged = MergedVideoInfo(record.output_dir, result.parent).data['files'][0]
        self.assertEqual((merged['quality_id'], merged['source_parts']), (80, [1, 2]))

    def test_list_formats_does_not_prompt_or_create_download_records(self) -> None:
        """验证画质查看模式不提示确认、不创建归档记录，字幕失败仍展示画质；无返回值。"""
        target = models.VideoTarget('https://www.bilibili.com/video/BV1xx411c7mD?p=1', 1, 1, 1)
        with patch.object(configuration, 'load_settings', return_value=self.settings), patch.object(auth, 'authenticated_session', return_value=MagicMock()), patch.object(auth, 'ensure_login', return_value={}), patch.object(api, 'resolve_targets', return_value=[target]), patch.object(download_service, 'make_downloader', return_value=self.downloader), patch.object(self.downloader, 'extract_info', return_value=sample_info()), patch.object(subtitle_service, 'list_subtitles', side_effect=errors.ToolError('字幕接口失败')), patch.object(workflow, 'print_video_info') as show, patch('builtins.input') as prompt, patch.object(download_service, 'transfer_video') as save:
            self.assertEqual(application.run(cli_entry.parse_args(['--url', target.url, '--list-formats'])), 2)
        prompt.assert_not_called()
        save.assert_not_called()
        show.assert_called_once()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_collection_merges_quality_folders_without_overwriting_other_posts(self) -> None:
        """验证两个投稿迁入同一合集清晰度目录时可并排共存，各自信息刷新不会登记对方视频；无返回值。"""
        collection = self.root / '演唱会_合集1'
        q = models.Quality(80, '1080P')
        for bvid in ('BV1xx411c7mD', 'BV1xx411c7mE'):
            record = metadata.open_video(self.root, bvid, '测试投稿', 1)
            folder = record.quality_directory(q)
            folder.mkdir(parents=True)
            path = folder / f'片段_{bvid}_p1.mp4'
            path.write_bytes(b'video')
            item = record.entry(f'{bvid}_p1', part=1, quality=q)
            record.record_file(item, path)
            record.save()
            metadata.move_to_collection(record, collection)
        with patch.object(metadata, 'probe_media', return_value={'errors': []}):
            record.refresh_local('ffmpeg')
        self.assertEqual(len(record.data['files']), 1)
        self.assertEqual(len(list(collection.with_name(collection.name + '_' + q.directory + '_分P').glob('*.mp4'))), 2)


if __name__ == '__main__':
    unittest.main()
