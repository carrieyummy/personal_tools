"""离线验证策略配置、重叠拼接组、多画质多编码保存、刷新锁定和旧版本复用。"""

from dataclasses import replace
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from yt_dlp import YoutubeDL

from bilibili_downloader import config, errors, library, media, metadata, models, quality, variants
from bilibili_downloader import merge as merging
from bilibili_downloader.merged_records import MergedVideoInfo
from tests import CONFIG_PATH
from tests import test_batch_download as batch
from tests.test_video_quality import sample_info


class PriorityTests(unittest.TestCase):
    """使用现有离线业务驱动器，验证真实调度及磁盘事务而不连接账号。"""

    def setUp(self) -> None:
        """创建隔离驱动器并启用拼接优先；无参数和返回值。"""
        self.engine = batch.BatchEngineTests()
        self.engine.setUp()
        self.addCleanup(self.engine.doCleanups)
        self.engine.settings = replace(self.engine.settings, priority='merge')
        self.enterContext(patch.object(config, 'load_settings', return_value=self.engine.settings))

    def run_list(self, entries: list[dict], selection: str = '0') -> int:
        """用 entries 清单执行 selection 拼接，返回实际入口退出码。"""
        return self.engine.run_list(entries, options={'merge-parts': selection})

    def record(self, bvid: str | None = None) -> metadata.VideoInfo:
        """读取 bvid 或首个测试投稿的持久化索引，返回聚合视图。"""
        return library.get_video_info(self.engine.settings, bvid or self.engine.ids[0])

    def test_overlapping_groups_download_multiple_qualities_and_reuse(self) -> None:
        """验证重叠组独立选档、重复组复用传输、重新运行及布局切换均保留六个来源版本；无返回值。"""
        engine, bvid = self.engine, self.engine.ids[0]
        engine.counts[bvid] = 4
        engine.catalogs = {f'{bvid}_p1': (120,), f'{bvid}_p2': (80, 120),
                           f'{bvid}_p3': (80, 120), f'{bvid}_p4': (80,)}
        entries, selection = [{'url': bvid}], '1-3;2-4;1-3'
        self.assertEqual(self.run_list(entries, selection), 0, engine.stderr.getvalue())
        record = self.record()
        self.assertEqual(len(record.data['files']), 6)
        self.assertEqual(sum(engine.calls.values()), 6)
        self.assertEqual({item['quality_id'] for item in record.data['files'] if item['part'] == 2}, {80, 120})
        paths = {record.resolve_file(item) for item in record.data['files']}
        self.assertEqual(len(paths), 6)
        self.assertTrue(all(path.is_file() for path in paths))
        results = MergedVideoInfo.for_results(record).data['merge_results']
        self.assertEqual([row['quality_id'] for row in results], [120, 80, 120])
        self.assertTrue(all(len(row['source_entries']) == 3 for row in results))
        self.assertEqual(self.run_list(entries, selection), 0, engine.stderr.getvalue())
        self.assertEqual(sum(engine.calls.values()), 6)
        for mixed in (False, True):
            with patch.object(config, 'load_settings', return_value=replace(engine.settings, mix_quality=mixed)):
                self.assertEqual(self.run_list(entries, selection), 0, engine.stderr.getvalue())
            self.assertEqual(sum(engine.calls.values()), 6)
        with patch.object(config, 'load_settings', return_value=replace(engine.settings, priority='quality')):
            self.assertEqual(self.run_list(entries, '-1'), 0, engine.stderr.getvalue())
        self.assertEqual(sum(engine.calls.values()), 6)
        self.assertEqual(len(self.record().data['files']), 6)
        log = engine.read_log()['runs'][0]['entries']
        downloads = [item for item in log if item['kind'] == '下载']
        self.assertEqual(len(downloads), 6)
        self.assertEqual({item['source_part'] for item in downloads}, {1, 2, 3, 4})
        self.assertTrue(all(item['groups'] for item in downloads))

    def test_same_quality_different_codecs_coexist_and_offline_selection(self) -> None:
        """验证两个组分别要求AVC/HEVC时重叠P保留两编码，离线拼接按组自动定位兼容版本；无返回值。"""
        engine, bvid = self.engine, self.engine.ids[0]
        engine.counts[bvid] = 3
        original = batch.FakeDownloader.extract_info

        def extract(downloader: batch.FakeDownloader, url: str, **options: object) -> dict:
            """按 url/options 生成指定P的编码列表；返回仍受真实选流约束的离线格式。"""
            info = original(downloader, url, **options)
            part = int(info['id'].rsplit('p', 1)[1])
            codecs = {1: ('avc1',), 2: ('avc1', 'hev1'), 3: ('hev1',)}[part]
            info['formats'] = [{**info['formats'][0], 'format_id': codec, 'vcodec': codec} for codec in codecs]
            return info

        with patch.object(batch.FakeDownloader, 'extract_info', new=extract):
            self.assertEqual(self.run_list([{'url': bvid}], '1-2;2-3'), 0, engine.stderr.getvalue())
        record = self.record()
        self.assertEqual(len(record.data['files']), 4)
        overlapping = [item for item in record.data['files'] if item['part'] == 2]
        self.assertEqual({item['formats'][0]['vcodec'] for item in overlapping}, {'avc1', 'hev1'})
        self.assertEqual(len({item['path'] for item in overlapping}), 2)
        self.assertEqual(merging.merge_groups(engine.settings, 'ffmpeg', record, '1-2;2-3'), 0)
        for mixed in (False, True):
            record.organize_qualities(mix_quality=mixed)
            reloaded = metadata.open_video(engine.settings.output_dir, bvid)
            self.assertEqual(len({reloaded.resolve_file(item) for item in reloaded.data['files']}), 4)
            self.assertTrue(all(reloaded.resolve_file(item).is_file() for item in reloaded.data['files']))

    def test_no_compatible_plan_still_downloads_and_other_bv_succeeds(self) -> None:
        """验证不兼容组按各P最高档下载并返回2，另一个BV独立成功，无终端交互；无返回值。"""
        engine = self.engine
        a, b = engine.ids
        engine.counts = {a: 2, b: 2}
        engine.catalogs = {f'{a}_p1': (120,), f'{a}_p2': (80,), f'{b}_p1': (80,), f'{b}_p2': (80,)}
        self.assertEqual(self.run_list([{'url': a}, {'url': b}]), 2)
        self.assertEqual(sum(engine.calls.values()), 4)
        self.assertEqual(MergedVideoInfo.for_results(self.record(a)).data['merge_results'][0]['status'], 'failed')
        self.assertEqual(MergedVideoInfo.for_results(self.record(b)).data['merge_results'][0]['status'], 'completed')
        engine.input.assert_not_called()

    def test_ceiling_and_ungrouped_parts(self) -> None:
        """验证quality排名仍按BV并集确定一次上限，组外P按上限内最高档下载；无返回值。"""
        engine, bvid = self.engine, self.engine.ids[0]
        engine.counts[bvid] = 3
        engine.catalogs = {f'{bvid}_p1': (64, 80, 120), f'{bvid}_p2': (64, 80), f'{bvid}_p3': (120,)}
        self.assertEqual(self.run_list([{'url': bvid, 'quality': 2}], '1-2'), 2)
        record = self.record()
        self.assertEqual({item['quality_id'] for item in record.data['files'] if item['status'] == 'completed'}, {80})
        self.assertEqual(MergedVideoInfo.for_results(record).data['merge_results'][0]['status'], 'completed')

    def test_refresh_does_not_change_locked_group_quality(self) -> None:
        """验证下载前4K消失时不单P降级破坏拼接组，其他来源仍下载；无返回值。"""
        engine, bvid = self.engine, self.engine.ids[0]
        engine.counts[bvid] = 2
        engine.catalogs = {f'{bvid}_p1': (80, 120), f'{bvid}_p2': (80, 120)}
        engine.dynamic = True
        self.assertEqual(self.run_list([{'url': bvid}]), 2)
        self.assertEqual(sum(engine.calls.values()), 0)
        self.assertTrue(all(item['status'] == 'failed' for item in self.record().data['files']))

    def test_legacy_sources_are_reused_without_renaming(self) -> None:
        """验证已有清晰度优先媒体转拼接优先时复用原路径及格式，不新增或重下相同版本；无返回值。"""
        engine, bvid = self.engine, self.engine.ids[0]
        engine.counts[bvid] = 2
        with patch.object(config, 'load_settings', return_value=replace(engine.settings, priority='quality')):
            self.assertEqual(self.run_list([{'url': bvid}]), 0)
        record = self.record()
        paths = {record.resolve_file(item) for item in record.data['files']}
        self.assertEqual(self.run_list([{'url': bvid}]), 0, engine.stderr.getvalue())
        self.assertEqual(sum(engine.calls.values()), 2)
        self.assertEqual({record.resolve_file(item) for item in self.record().data['files']}, paths)

    def test_one_version_failure_does_not_block_other_group(self) -> None:
        """验证仅高画质版本传输失败时，重叠P的低画质版本和另一组仍可成功；无返回值。"""
        engine, bvid = self.engine, self.engine.ids[0]
        engine.counts[bvid] = 3
        engine.catalogs = {f'{bvid}_p1': (120,), f'{bvid}_p2': (80, 120), f'{bvid}_p3': (80,)}
        original = engine.transfer

        def transfer(filename: str, info: dict, hooks: list) -> tuple[bool, bool]:
            """仅拒绝P2的4K info，保存 filename 断点；其他版本沿用 hooks 及正常传输结果。"""
            if info['id'] == f'{bvid}_p2' and info.get('quality') == 120:
                Path(filename + '.part').write_bytes(b'partial')
                raise errors.ToolError('指定版本网络失败')
            return original(filename, info, hooks)

        with patch.object(engine, 'transfer', side_effect=transfer):
            self.assertEqual(self.run_list([{'url': bvid}], '1-2;2-3'), 2)
        record = self.record()
        results = MergedVideoInfo.for_results(record).data['merge_results']
        self.assertEqual([item['status'] for item in results], ['failed', 'completed'])
        self.assertTrue(list(engine.settings.output_dir.rglob('*.part')))
        p2 = {item['quality_id']: item['status'] for item in record.data['files'] if item['part'] == 2}
        self.assertEqual(p2, {120: 'failed', 80: 'completed'})

    def test_cancellation_keeps_variant_partial_files(self) -> None:
        """验证多版本下载收到取消后退出130并保留格式隔离的断点；无返回值。"""
        engine, bvid = self.engine, self.engine.ids[0]
        engine.counts[bvid] = 3
        engine.catalogs = {f'{bvid}_p1': (120,), f'{bvid}_p2': (80, 120), f'{bvid}_p3': (80,)}
        engine.cancel = True
        self.assertEqual(self.run_list([{'url': bvid}], '1-2;2-3'), 130)
        self.assertTrue(list(engine.settings.output_dir.rglob('*.part')))
        self.assertTrue(all(item['status'] in ('interrupted', 'skipped') for item in engine.read_log()['runs'][0]['entries']
                            if item['kind'] == '下载'))

    def test_direct_link_confirms_ceiling_only_once_for_multiple_groups(self) -> None:
        """验证普通链接多个组只询问一次BV画质上限，不为组或编码额外交互；无返回值。"""
        engine, bvid = self.engine, self.engine.ids[0]
        engine.counts[bvid] = 3
        engine.catalogs = {f'{bvid}_p{part}': (80, 120) for part in (1, 2, 3)}
        with patch('builtins.input', return_value='1') as prompt:
            self.assertEqual(batch.cli.main(['--url', bvid, '--all-parts', '--merge-parts', '1-2;2-3']), 0,
                             engine.stderr.getvalue())
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(sum(engine.calls.values()), 3)

    def test_fallback_overlap_does_not_replace_successful_group_plan(self) -> None:
        """验证后一个不兼容组复用前组版本时，格式排序元数据不影响前组成功判定；无返回值。"""
        engine, bvid = self.engine, self.engine.ids[0]
        engine.counts[bvid] = 3
        engine.catalogs = {f'{bvid}_p1': (80,), f'{bvid}_p2': (80,), f'{bvid}_p3': (120,)}
        self.assertEqual(self.run_list([{'url': bvid}], '1-2;2-3'), 2)
        results = MergedVideoInfo.for_results(self.record()).data['merge_results']
        self.assertEqual([item['status'] for item in results], ['completed', 'failed'])
        self.assertEqual(sum(engine.calls.values()), 3)


class PriorityConfigAndSelectionTests(unittest.TestCase):
    """使用真实离线yt-dlp及隔离YAML验证格式选择约束与配置校验。"""

    def test_real_selector_uses_common_codec_without_exceeding_ceiling(self) -> None:
        """验证真实选流可在同档换用共有编码，并在分辨率不同时选择较低兼容档；无返回值。"""
        settings = config.load_settings(CONFIG_PATH)
        with YoutubeDL({'quiet': True, 'no_warnings': True, 'format': settings.format_selector,
                        'format_sort': settings.format_sort}) as downloader:
            catalogs = {}
            for part in (1, 2):
                info = sample_info(part)
                if part == 2:
                    info['formats'] = [fmt for fmt in info['formats'] if fmt['vcodec'] != 'hev1']
                    for fmt in info['formats']:
                        if fmt.get('quality') == 120:
                            fmt['height'] = 2000
                catalogs[part] = quality.available_variants(downloader, info, quality.available_qualities(downloader, info))
            choice = quality.common_choice(catalogs, [1, 2], next(iter(catalogs[1][120].values())).quality)
            self.assertEqual({value.quality.code for value in choice.values()}, {80})
            self.assertEqual(len({variants.compatibility(list(value.formats)) for value in choice.values()}), 1)
            self.assertEqual(choice[1].formats[0]['vcodec'], 'avc1')

    def test_priority_required_and_enum_checked(self) -> None:
        """验证priority合法值、空值、错误类型及缺项；读取当前配置构造隔离文件，不依赖用户具体配置值。"""
        original = CONFIG_PATH.read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            for value in ('quality', 'merge', '', 'true', '[]', 'bad'):
                path.write_text(re.sub(r'(?m)^  priority:.*$', f'  priority: {value}', original), encoding='utf-8')
                with self.subTest(value=value):
                    if value in ('quality', 'merge'):
                        self.assertEqual(config.load_settings(path).priority, value)
                    else:
                        with self.assertRaises(errors.ToolError):
                            config.load_settings(path)
            path.write_text(re.sub(r'(?m)^  priority:.*\n', '', original), encoding='utf-8')
            with self.assertRaises(errors.ToolError):
                config.load_settings(path)

    def test_real_ffmpeg_merges_explicit_overlapping_variants(self) -> None:
        """用真实短视频验证同P不同尺寸版本按组精确拼接，输出逐帧哈希和来源顺序保持一致；无返回值。"""
        settings = config.load_settings(CONFIG_PATH)
        executable = media.resolve_ffmpeg(settings, CONFIG_PATH.parent)
        bvid, q = 'BV1xx411c7mD', models.Quality(80, '1080P')
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(settings, output_dir=Path(directory) / 'downloads')
            record = library.get_video_info(settings, bvid, '版本测试', 3)
            record.data.update(errors=[], current_parts=[{'part': part, 'cid': part} for part in (1, 2, 3)])
            plans, expected = [], []
            for parts, size in (([1, 2], 64), ([2, 3], 96)):
                choice = variants.StreamChoice(q, ({'format_id': 'raw', 'vcodec': 'avc1', 'acodec': 'none',
                                                   'width': size, 'height': size, 'fps': 5},))
                plans.append(variants.MergePlan(','.join(map(str, parts)), parts, dict.fromkeys(parts, choice)))
                sources = []
                for part in parts:
                    folder = record.quality_directory(q)
                    folder.mkdir(parents=True, exist_ok=True)
                    path = folder / f'{bvid}_p{part}_fmt{choice.variant[:12]}.mp4'
                    result = subprocess.run([executable, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                                             f'color=c={"red" if part % 2 else "blue"}:size={size}x{size}:rate=5:duration=0.4',
                                             '-c:v', 'libx264', '-threads', '1', '-an', str(path)], capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    item = record.entry(f'{bvid}_p{part}', part=part, quality=q, variant=choice.variant)
                    item.update(formats=list(choice.formats), source_cid=part, base_stem=path.stem,
                                mix_quality=record.mix_quality)
                    record.record_file(item, path)
                    sources.append(path)
                expected.append(sources)
            record.save()
            self.assertEqual(merging.merge_plans(settings, executable, record, plans), 0)
            index = MergedVideoInfo(record.output_dir, record.quality_directory(q, role='merged'))

            def frames(path: Path) -> list[str]:
                """解码 path 并返回有序帧哈希，以验证流复制拼接结果。"""
                result = subprocess.run([executable, '-hide_banner', '-loglevel', 'error', '-i', str(path),
                                         '-map', '0:v:0', '-f', 'framemd5', '-'], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return [line.rsplit(',', 1)[-1].strip() for line in result.stdout.splitlines() if line and not line.startswith('#')]

            self.assertEqual(len(index.data['files']), 2)
            for item, paths in zip(index.data['files'], expected):
                self.assertEqual(frames(index.resolve_file(item)), [frame for path in paths for frame in frames(path)])
                self.assertEqual(len(item['source_entries']), 2)
                self.assertTrue(all(path.is_file() for path in paths))
