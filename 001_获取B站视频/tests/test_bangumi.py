"""离线验证番剧解析、原始播放流、季归档、续传和普通投稿索引兼容，不读取真实账号。"""

from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from yt_dlp import YoutubeDL

import download_bilibili as cli
from bilibili_downloader import api, application, auth, bangumi, config, errors, library, manifest, media, metadata, runtime, subtitles
from bilibili_downloader import downloader as transfer
from bilibili_downloader.merged_records import MergedVideoInfo
from bilibili_downloader.models import entry_quality
from tests import CONFIG_PATH, fake_auth_snapshot
from tests.test_download_bilibili import FakeResponse


def season_fixture() -> dict:
    """返回包含十一集正片和一个预告的模拟原站季数据，保留用户链接在正片列表中的 P10 位置。"""
    ids = [285053, 285630, 287592, 289351, 291707, 292787, 299870, 304430, 308563, 311310, 312273]
    episodes = [{'id': ep, 'cid': 1000 + part, 'aid': 2000 + part, 'bvid': f'BV1xx411c7{part:02d}',
                 'title': str(part), 'long_title': f'样例剧集{part}'} for part, ep in enumerate(ids, 1)]
    return {'season_id': 28541, 'title': '测试番剧', 'episodes': episodes, 'seasons': [{'season_id': 28541}],
            'section': [{'id': 38504, 'title': '预告', 'episodes': [
                {'id': 399999, 'cid': 1999, 'aid': 2999, 'bvid': None, 'title': '预告', 'long_title': ''}]}]}


def playback_fixture(codes: tuple[int, ...] = (80,)) -> dict:
    """按正整数画质 codes 构造无 DRM 的完整 PGC DASH 响应，供真实 yt-dlp 格式解析器读取。"""
    return {'play_check': {'play_detail': 'PLAY_WHOLE'}, 'video_info': {
        'code': 0, 'is_preview': 0, 'is_drm': False, 'timelength': 2000,
        'support_formats': [{'quality': code, 'new_description': f'画质{code}'} for code in codes],
        'dash': {'video': [{'id': code, 'baseUrl': f'https://example.com/video-{code}.m4s?test=1',
                            'mimeType': 'video/mp4', 'codecs': 'avc1.640028', 'bandwidth': 100000,
                            'width': 64, 'height': 64, 'frameRate': '5'} for code in codes],
                 'audio': [{'id': 30280, 'baseUrl': 'https://example.com/audio.m4s', 'mimeType': 'audio/mp4',
                            'codecs': 'mp4a.40.2', 'bandwidth': 64000}]}}}


class BangumiApiTests(unittest.TestCase):
    """校验真实链接形态、PGC响应和字幕参数，所有原站 HTTP 均由内存响应替代。"""

    def setUp(self) -> None:
        """读取项目实际配置，创建模拟认证会话及季数据；无参数及返回值。"""
        self.settings = config.load_settings(CONFIG_PATH)
        self.context = MagicMock()
        self.season = season_fixture()
        self.context.request.get.return_value = FakeResponse({'code': 0, 'result': self.season})

    def target(self) -> object:
        """返回用户链接对应的模拟 P10 下载目标；无参数。"""
        return api.resolve_targets(self.context, 'ep311310', self.settings, False)[0]

    def test_episode_season_share_and_bare_links(self) -> None:
        """验证分享参数和尾斜杠被规范化，裸 ep/ss 和移动端链接可以输入；无返回值。"""
        canonical = 'https://www.bilibili.com/bangumi/play/ep311310'
        for raw in ('ep311310', canonical + '?from_spmid=666.25.episode.0',
                    '分享 ' + canonical + '/', 'https://m.bilibili.com/bangumi/play/ep311310'):
            with self.subTest(raw=raw):
                self.assertEqual(api.normalize_url(raw), canonical)
        self.assertEqual(api.normalize_url('SS28541'), 'https://www.bilibili.com/bangumi/play/ss28541')
        for raw in ('ep0', 'ss-1', 'https://www.bilibili.com/bangumi/play/ep311310evil'):
            with self.subTest(raw=raw), self.assertRaises(errors.ToolError):
                api.normalize_url(raw)

    def test_requested_episode_keeps_season_part_and_real_identifiers(self) -> None:
        """验证 ep311310 按完整季映射保留 P10，并携带真实剧集身份而非伪造 BV；无返回值。"""
        target = self.target()
        self.assertEqual((target.part, target.total_parts, target.source_id, target.episode_id), (10, 11, 'ss28541', 311310))
        self.assertEqual(target.bvid, self.season['episodes'][9]['bvid'])
        self.assertIsNone(target.metadata['bvid'])
        self.assertEqual(target.metadata['source_type'], 'bangumi')
        self.assertEqual(self.context.request.get.call_args.kwargs['params'], {'ep_id': 311310})

    def test_season_first_episode_and_all_main_episodes(self) -> None:
        """验证 ss 默认第一集，有效全部开关才选整季，正片顺序与编号不受预告影响；无返回值。"""
        self.assertEqual(api.resolve_targets(self.context, 'ss28541', self.settings, False)[0].part, 1)
        for url in ('ss28541', 'ep311310'):
            targets = api.resolve_targets(self.context, url, self.settings, True)
            self.assertEqual([target.part for target in targets], list(range(1, 12)))
            self.assertNotIn(399999, [target.episode_id for target in targets])

    def test_extra_episode_has_independent_single_part_record(self) -> None:
        """验证显式预告 ep 单独归档，全部开关仍只选择所属季正片；无返回值。"""
        target = api.resolve_targets(self.context, 'ep399999', self.settings, False)[0]
        self.assertEqual((target.source_id, target.part, target.total_parts, target.aid), ('ep399999', 1, 1, 2999))
        self.assertIsNone(target.bvid)
        self.assertEqual(len(api.resolve_targets(self.context, 'ep399999', self.settings, True)), 11)

    def test_short_link_resolves_to_bangumi(self) -> None:
        """验证 b23 分享跳转到番剧后继续请求季接口；无返回值。"""
        self.context.request.get.side_effect = [FakeResponse({}, url='https://www.bilibili.com/bangumi/play/ep311310'),
                                                 FakeResponse({'code': 0, 'result': self.season})]
        self.assertEqual(api.resolve_targets(self.context, 'https://b23.tv/abc', self.settings, False)[0].part, 10)

    def test_related_seasons_preserve_order_deduplicate_and_include_current(self) -> None:
        """验证关联季按原站顺序去重，列表漏掉当前季时补入，空列表仅保留当前季；无返回值。"""
        self.season['seasons'] = [{'season_id': 9}, {'season_id': 7}, {'season_id': 9}]
        self.assertEqual(bangumi.related_season_urls(self.context, api.normalize_url('ep311310'), self.settings),
                         [api.normalize_url(value) for value in ('ss9', 'ss7', 'ss28541')])
        self.season['seasons'] = []
        self.assertEqual(bangumi.related_season_urls(self.context, api.normalize_url('ss28541'), self.settings),
                         [api.normalize_url('ss28541')])

    def test_invalid_related_seasons_fail_without_silent_single_season(self) -> None:
        """验证缺少关联列表、非法季编号或结构明确报错，不静默缩小下载范围；无返回值。"""
        for value in (None, {}, [None], [{}], [{'season_id': 0}], [{'season_id': True}], [{'season_id': '9'}]):
            self.season['seasons'] = value
            with self.subTest(value=value), self.assertRaises(errors.ToolError):
                bangumi.related_season_urls(self.context, api.normalize_url('ss28541'), self.settings)

    def test_expand_short_link_preserves_options_and_leaves_video_scope(self) -> None:
        """验证短链展开整季时保留画质和拼接选项，普通投稿仍保留原分P范围；无返回值。"""
        request = manifest.DownloadRequest('https://b23.tv/abc', '备注', False, '1,2', 2, '测试清单')
        self.context.request.get.side_effect = [FakeResponse({}, url=api.normalize_url('ep311310')),
                                                 FakeResponse({'code': 0, 'result': self.season})]
        expanded = application.expand_season_request(self.context, request, self.settings)
        self.assertEqual([(item.url, item.all_parts, item.merge_parts, item.quality_rank, item.title) for item in expanded],
                         [(api.normalize_url('ss28541'), True, '1,2', 2, '备注')])
        self.context.request.get.reset_mock()
        video = replace(request, url='https://www.bilibili.com/video/BV1xx411c7mD?p=2')
        self.assertEqual(application.expand_season_request(self.context, video, self.settings), [video])
        self.context.request.get.assert_not_called()

    def test_all_seasons_config_requires_boolean_and_preserves_relative_paths(self) -> None:
        """验证独立开关严格读取 YAML 布尔值且必填，迁移后的相对路径仍以配置目录定位；无返回值。"""
        import yaml

        original = yaml.safe_load(CONFIG_PATH.read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'config' / 'custom.yaml'
            path.parent.mkdir()
            for value in (True, False, 'true', 1, None):
                data = deepcopy(original)
                data['paths']['output_dir'] = '../media'
                data['auth']['cookie_file'] = '../auth/cookies*.json'
                data['download']['all_seasons'] = value
                path.write_text(yaml.safe_dump(data), encoding='utf-8')
                with self.subTest(value=value):
                    if type(value) is bool:
                        settings = config.load_settings(path)
                        self.assertIs(settings.all_seasons, value)
                        self.assertEqual(settings.output_dir, root / 'media')
                        self.assertEqual(settings.cookie_file, root / 'auth' / 'cookies*.json')
                    else:
                        with self.assertRaises(errors.ToolError):
                            config.load_settings(path)
            del data['download']['all_seasons']
            path.write_text(yaml.safe_dump(data), encoding='utf-8')
            with self.assertRaises(errors.ToolError):
                config.load_settings(path)

    def test_malformed_missing_and_duplicate_episodes_are_reported(self) -> None:
        """验证未知剧集、季不匹配、重复 EP 和非法 CID 明确失败，不推测来源；无返回值。"""
        for change, url in ((lambda season: season['episodes'][0].update(cid=0), 'ss28541'),
                            (lambda season: season['episodes'][1].update(id=season['episodes'][0]['id']), 'ss28541'),
                            (lambda season: season.update(season_id=1), 'ss28541'),
                            (lambda season: None, 'ep123456')):
            season = deepcopy(self.season)
            change(season)
            self.context.request.get.return_value = FakeResponse({'code': 0, 'result': season})
            with self.subTest(url=url), self.assertRaises(errors.ToolError):
                api.resolve_targets(self.context, url, self.settings, True)

    def test_full_streams_use_pgc_and_shared_format_parser(self) -> None:
        """验证 PGC 完整流可通过真实解析器生成带画质的单集格式表，Referer 与 EP/CID 参数正确；无返回值。"""
        target = self.target()
        self.context.request.get.return_value = FakeResponse({'code': 0, 'result': playback_fixture((80, 120))})
        with YoutubeDL({'quiet': True}) as downloader:
            info = bangumi.extract_info(self.context, downloader, target, self.settings)
        self.assertEqual(info['id'], 'ss28541_p10')
        self.assertEqual({fmt['quality'] for fmt in info['formats'] if fmt.get('vcodec') != 'none'}, {80, 120})
        call = self.context.request.get.call_args
        self.assertEqual(call.kwargs['params']['ep_id'], target.episode_id)
        self.assertEqual(call.kwargs['params']['cid'], target.cid)
        self.assertEqual(call.kwargs['headers']['Referer'], target.url)

    def test_preview_drm_and_access_errors_do_not_become_full_downloads(self) -> None:
        """验证试看、DRM、无流、无权限和缺失 result 均被明确拒绝；无返回值。"""
        target = self.target()
        preview, drm, empty = (playback_fixture() for _ in range(3))
        preview['video_info']['is_preview'] = 1
        drm['video_info']['is_drm'] = True
        empty['video_info']['dash'] = {}
        payloads = [{'code': 0, 'result': value} for value in (preview, drm, empty)]
        payloads += [{'code': -10403, 'message': '需要大会员'}, {'code': 0, 'data': playback_fixture()}]
        with YoutubeDL({'quiet': True}) as downloader:
            for payload in payloads:
                self.context.request.get.return_value = FakeResponse(payload)
                with self.subTest(payload=payload), self.assertRaises(errors.ToolError):
                    bangumi.extract_info(self.context, downloader, target, self.settings)

    def test_subtitle_query_uses_real_aid_and_cid(self) -> None:
        """验证字幕使用每集 AV/CID，并沿用配置中的语言与 AI 规则；无返回值。"""
        target = self.target()
        self.context.request.get.return_value = FakeResponse({'code': 0, 'data': {'subtitle': {'subtitles': []}}})
        self.assertEqual(subtitles.list_subtitles(self.context, target, {'id': 'ss28541_p10'}, self.settings), [])
        params = self.context.request.get.call_args.kwargs['params']
        self.assertEqual(params, {'aid': target.aid, 'cid': target.cid, 'ep_id': target.episode_id})


class BangumiIntegrationTests(unittest.TestCase):
    """走真实解析、选流、调度、文件事务及索引；仅替代远程 HTTP、媒体数据和 FFmpeg。"""

    def setUp(self) -> None:
        """创建隔离下载目录和三个正片的模拟原站，并接入真实 yt-dlp 格式选择；无返回值。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = replace(config.load_settings(CONFIG_PATH), output_dir=self.root / 'downloads',
                                all_parts=False, all_seasons=False, threads=1, priority='quality', overwrite_merged=False)
        self.season = season_fixture()
        self.season['episodes'] = self.season['episodes'][:3]
        self.seasons = {self.season['season_id']: self.season}
        self.context = MagicMock()
        self.context.request.get.side_effect = self.remote
        self.calls, self.catalogs, self.fail_parts, self.blocked = Counter(), {}, set(), set()
        self.paths, self.resumed = [], []
        self.stdout = self.enterContext(redirect_stdout(StringIO()))
        self.stderr = self.enterContext(redirect_stderr(StringIO()))
        self.enterContext(patch.object(auth, 'authenticated_session', return_value=MagicMock(
            __enter__=MagicMock(return_value=self.context))))
        self.enterContext(patch.object(auth, 'ensure_login', return_value={'uname': 'test'}))
        self.enterContext(patch.object(auth, 'snapshot_session', return_value=fake_auth_snapshot()))
        self.enterContext(patch.object(auth.AuthenticatedHttpSession, 'get', side_effect=self.context.request.get))
        self.enterContext(patch.object(transfer, 'make_downloader', side_effect=self.make_downloader))
        self.enterContext(patch.object(media, 'resolve_ffmpeg', return_value='ffmpeg'))
        self.enterContext(patch.object(media, 'mux_streams', side_effect=self.mux))
        self.enterContext(patch.object(metadata, 'probe_media', return_value={
            'duration_seconds': 2, 'width': 64, 'height': 64, 'fps': 5,
            'video_codec': 'h264', 'audio_codec': 'aac', 'errors': []}))
        self.concatenate = self.enterContext(patch.object(runtime, 'run_process', side_effect=self.concat))
        self.prompt = self.enterContext(patch('builtins.input', side_effect=AssertionError('清单不应弹出画质菜单')))

    def remote(self, url: str, **options: object) -> FakeResponse:
        """按 url/options 返回模拟季、播放或字幕响应，保留实际 api_json 校验路径；返回 FakeResponse。"""
        if url.endswith('/pgc/view/web/season'):
            params = options['params']
            season = self.seasons.get(params.get('season_id')) if 'season_id' in params else next(
                (item for item in self.seasons.values() if any(ep['id'] == params['ep_id']
                 for ep in item['episodes'] + [ep for section in item.get('section', []) for ep in section['episodes']])), None)
            return FakeResponse({'code': 0, 'result': deepcopy(season)} if season else {'code': -404, 'message': '季不存在'})
        if url.endswith('/pgc/player/web/v2/playurl'):
            ep = options['params']['ep_id']
            if ep in self.blocked:
                return FakeResponse({'code': -10403, 'message': '需要观看权限'})
            return FakeResponse({'code': 0, 'result': playback_fixture(self.catalogs.get(ep, (80,)))})
        if url.endswith('/x/player/wbi/v2'):
            return FakeResponse({'code': 0, 'data': {'subtitle': {'subtitles': []}}})
        raise AssertionError(f'意外原站请求：{url}')

    def make_downloader(self, context: object, settings: config.Settings) -> YoutubeDL:
        """为 context/settings 创建独立真实选流器并替代媒体传输，返回可关闭的 YoutubeDL。"""
        downloader = YoutubeDL({'format': settings.format_selector, 'format_sort': settings.format_sort,
                                'quiet': True, 'noprogress': True, 'proxy': settings.proxy})
        downloader.dl = self.download_stream
        return downloader

    def download_stream(self, filename: str, info: dict) -> tuple[bool, bool]:
        """把 info 写至 filename；指定失败时保留断点，记录真实调用和可续传路径，返回成功标识。"""
        path = Path(filename)
        self.paths.append(path)
        self.calls[info['id']] += 1
        partial = path.with_name(path.name + '.part')
        if partial.exists():
            self.resumed.append(path)
        if int(info['id'].rsplit('_p', 1)[1]) in self.fail_parts:
            partial.write_bytes(b'partial')
            raise errors.ToolError('模拟网络中断')
        path.write_bytes(info['id'].encode())
        partial.unlink(missing_ok=True)
        return True, True

    def mux(self, executable: str, sources: list[Path], formats: list[dict], folder: Path, stem: str) -> Path:
        """模拟 executable 将 sources/formats 封装至 folder/stem，返回非空媒体路径。"""
        path = folder / (stem + '.mp4')
        path.write_bytes(b''.join(source.read_bytes() for source in sources))
        return path

    def concat(self, command: list[str], **options: object) -> subprocess.CompletedProcess:
        """按 command 模拟 FFmpeg 合并，options 为兼容进程参数，返回成功结果。"""
        Path(command[-1]).write_bytes(b'merged')
        return subprocess.CompletedProcess(command, 0, '', '')

    def run_list(self, url: str = 'ss28541', *, all_parts: bool = True, merge: str = '-1', quality_rank: int = 1) -> int:
        """以 url/all_parts/merge/quality_rank 写临时清单并执行真实应用流程，返回退出码；不读取真实账号及目录。"""
        import yaml

        manifest = self.root / 'tasks.yaml'
        manifest.write_text(yaml.safe_dump({'quality': quality_rank, 'all-parts': all_parts, 'merge-parts': merge,
                                            'download_list': [{'url': url}]}), encoding='utf-8')
        return application.run_with_settings(cli.parse_args(['--url', str(manifest)]), self.settings, None)

    def record(self) -> metadata.VideoInfo:
        """返回当前临时目录内番剧季的聚合源记录；无参数。"""
        return library.get_video_info(self.settings, 'ss28541')

    def add_related_season(self) -> dict:
        """添加与当前季同标题但独立 EP/CID/ss 的模拟季，关联列表含重复项，返回新增季字典；无参数。"""
        other = deepcopy(self.season)
        other['season_id'] += 1
        other['section'] = []
        for episode in other['episodes']:
            for key in ('id', 'cid', 'aid'):
                episode[key] += 1_000_000
        self.seasons[other['season_id']] = other
        related = [{'season_id': other['season_id']}, {'season_id': self.season['season_id']},
                   {'season_id': other['season_id']}]
        self.season['seasons'] = other['seasons'] = related
        return other

    def test_all_seasons_download_and_merge_independently_then_reuse(self) -> None:
        """验证单个 ep 输入强制展开全部季，按 ss 隔离同标题目录与合并结果，并可复用成功媒体；无返回值。"""
        self.add_related_season()
        self.settings = replace(self.settings, all_seasons=True, mix_quality=True)
        ep = self.season['episodes'][1]['id']
        self.assertEqual(self.run_list(f'ep{ep}', all_parts=False, merge='0'), 0, self.stderr.getvalue())
        for season in self.seasons.values():
            source_id = f"ss{season['season_id']}"
            record = library.get_video_info(self.settings, source_id)
            self.assertEqual([item['part'] for item in record.data['files']], [1, 2, 3])
            self.assertTrue(all(item['status'] == 'completed' for item in record.data['files']))
            self.assertEqual({item['episode_id'] for item in record.data['files']}, {item['id'] for item in season['episodes']})
            self.assertEqual({record.resolve_file(item).parent.name for item in record.data['files']},
                             {f"{season['title']}_{source_id}_分P"})
            merged = MergedVideoInfo(self.settings.output_dir, record.base_directory.with_name(record.base_directory.name + '_合并'))
            self.assertEqual(len(merged.data['files']), 1)
            self.assertEqual(merged.data['files'][0]['source_id'], source_id)
            self.assertEqual(merged.data['files'][0]['source_parts'], [1, 2, 3])
        self.assertEqual(len(list(self.settings.output_dir.glob('*_分P'))), 2)
        previous = self.calls.copy()
        self.assertEqual(self.run_list(all_parts=False, merge='0'), 0, self.stderr.getvalue())
        self.assertEqual(self.calls, previous)
        self.prompt.assert_not_called()

    def test_all_seasons_disabled_keeps_requested_episode(self) -> None:
        """验证关闭独立开关时，存在关联季也只下载链接指定集；无返回值。"""
        other = self.add_related_season()
        ep = self.season['episodes'][1]['id']
        self.assertEqual(self.run_list(f'ep{ep}', all_parts=False), 0, self.stderr.getvalue())
        self.assertEqual([item['part'] for item in self.record().data['files']], [2])
        self.assertFalse(metadata.find_video_folders(self.settings.output_dir, f"ss{other['season_id']}"))

    def test_all_seasons_failure_keeps_other_season_downloads(self) -> None:
        """验证关联季无可下载正片时仍继续其余季，成功文件保留且返回未全部完成；无返回值。"""
        other = self.add_related_season()
        other['episodes'] = []
        self.settings = replace(self.settings, all_seasons=True)
        self.assertEqual(self.run_list(all_parts=False), 2)
        self.assertIn(f"ss{other['season_id']}", self.stderr.getvalue())
        self.assertEqual([item['part'] for item in self.record().data['files']], [1, 2, 3])
        self.assertTrue(all(item['status'] == 'completed' for item in self.record().data['files']))

    def test_all_seasons_duplicate_inputs_choose_rank_once(self) -> None:
        """验证同系列重复 ep/ss 输入共用一次档位排名确认，每集只传输一份音视频；无返回值。"""
        self.add_related_season()
        self.settings = replace(self.settings, all_seasons=True)
        args = cli.parse_args(['--url', f"ep{self.season['episodes'][1]['id']}", '--url', 'ss28541'])
        with patch('builtins.input', return_value='1') as prompt:
            self.assertEqual(application.run_with_settings(args, self.settings, None), 0, self.stderr.getvalue())
        prompt.assert_called_once()
        self.assertEqual(self.calls, {f'ss{identifier}_p{part}': 2 for identifier in self.seasons for part in (1, 2, 3)})

    def test_all_seasons_first_rank_downloads_each_episode_highest(self) -> None:
        """验证同组各集档位不同仍分别取最高，不把较早季画质或固定编号当作全部季的上限；无返回值。"""
        other = self.add_related_season()
        self.settings = replace(self.settings, all_seasons=True)
        self.catalogs = {other['episodes'][0]['id']: (80, 125), self.season['episodes'][0]['id']: (80, 120),
                         self.season['episodes'][1]['id']: (64,)}
        with patch('builtins.input', side_effect=['1']) as prompt:
            self.assertEqual(application.run_with_settings(cli.parse_args(['--url', 'ss28541', '--all-parts']),
                                                           self.settings, None), 0, self.stderr.getvalue())
        prompt.assert_called_once()
        self.assertEqual({item['part']: item['quality_id'] for item in self.record().data['files']}, {1: 120, 2: 64, 3: 80})
        record = library.get_video_info(self.settings, f"ss{other['season_id']}")
        self.assertEqual({item['part']: item['quality_id'] for item in record.data['files']}, {1: 125, 2: 80, 3: 80})
        self.assertIn('本档支持 6/6 集', self.stdout.getvalue())
        queries = Counter(call.kwargs['params']['ep_id'] for call in self.context.request.get.call_args_list
                          if call.args[0].endswith('/pgc/player/web/v2/playurl'))
        self.assertEqual(set(queries.values()), {2}, '每集只查询初始档位及派发前流，不为共享排名重复查询')

    def test_all_seasons_second_rank_is_per_episode(self) -> None:
        """验证输入2按每集自己的档位排名选择，既不共享画质编号，也不按每季并集排名；无返回值。"""
        other = self.add_related_season()
        self.settings = replace(self.settings, all_seasons=True)
        self.catalogs = {episode['id']: (80, 125) for episode in other['episodes']}
        self.catalogs.update({episode['id']: codes for episode, codes in zip(
            self.season['episodes'], ((80, 120), (64, 80), (32, 64)))})
        with patch('builtins.input', side_effect=['2']) as prompt:
            self.assertEqual(application.run_with_settings(cli.parse_args(['--url', 'ss28541']), self.settings, None),
                             0, self.stderr.getvalue())
        prompt.assert_called_once()
        self.assertEqual({item['part']: item['quality_id'] for item in self.record().data['files']}, {1: 80, 2: 64, 3: 32})
        record = library.get_video_info(self.settings, f"ss{other['season_id']}")
        self.assertEqual({item['quality_id'] for item in record.data['files']}, {80})

    def test_all_seasons_missing_rank_only_fails_affected_episode(self) -> None:
        """验证某集缺少第二档时只报告该集失败，其他季和剧集继续下载，不改为最高档；无返回值。"""
        self.add_related_season()
        self.settings = replace(self.settings, all_seasons=True)
        self.catalogs = {episode['id']: (80, 120) for season in self.seasons.values() for episode in season['episodes']}
        self.catalogs[self.season['episodes'][1]['id']] = (80,)
        with patch('builtins.input', side_effect=['2']) as prompt:
            self.assertEqual(application.run_with_settings(cli.parse_args(['--url', 'ss28541']), self.settings, None), 2)
        prompt.assert_called_once()
        self.assertNotIn('ss28541_p2', self.calls)
        self.assertEqual(len(self.calls), 5)
        self.assertIn('ss28541 P2：画质排名 2 不存在', self.stderr.getvalue())

    def test_all_seasons_refresh_reapplies_rank_instead_of_old_quality(self) -> None:
        """验证确认排名后新出现更高档位，派发时使用最新最高档，保持只确认一次；无返回值。"""
        self.add_related_season()
        self.settings = replace(self.settings, all_seasons=True)

        def choose_rank(prompt: str) -> str:
            """响应 prompt 并模拟确认后所有集出现更高画质；返回排名1。"""
            for season in self.seasons.values():
                self.catalogs.update({episode['id']: (80, 120) for episode in season['episodes']})
            return '1'

        with patch('builtins.input', side_effect=choose_rank) as prompt:
            self.assertEqual(application.run_with_settings(cli.parse_args(['--url', 'ss28541']), self.settings, None),
                             0, self.stderr.getvalue())
        prompt.assert_called_once()
        for season in self.seasons.values():
            record = library.get_video_info(self.settings, f"ss{season['season_id']}")
            self.assertEqual({item['quality_id'] for item in record.data['files'] if item['status'] == 'completed'}, {120})

    def test_all_seasons_unrelated_series_choose_independently(self) -> None:
        """验证同标题的另一番剧按独立ss组确认，可以分别选择最高和第二高；无返回值。"""
        self.add_related_season()
        independent = deepcopy(self.season)
        independent['season_id'] += 2
        independent['section'] = []
        independent['seasons'] = [{'season_id': independent['season_id']}]
        for episode in independent['episodes']:
            for key in ('id', 'cid', 'aid'):
                episode[key] += 2_000_000
        self.seasons[independent['season_id']] = independent
        self.settings = replace(self.settings, all_seasons=True)
        self.catalogs = {episode['id']: (80, 120) for season in self.seasons.values() for episode in season['episodes']}
        args = cli.parse_args(['--url', 'ss28541', '--url', f"ss{independent['season_id']}"])
        with patch('builtins.input', side_effect=['2', '1']) as prompt:
            self.assertEqual(application.run_with_settings(args, self.settings, None), 0, self.stderr.getvalue())
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual({item['quality_id'] for item in self.record().data['files']}, {80})
        record = library.get_video_info(self.settings, f"ss{independent['season_id']}")
        self.assertEqual({item['quality_id'] for item in record.data['files']}, {120})

    def test_all_seasons_overlapping_expansions_share_rank(self) -> None:
        """验证不同输入的关联列表部分重叠时归并同组，保留两次展开的成员且只询问一次；无返回值。"""
        other = self.add_related_season()
        other['seasons'] = [{'season_id': other['season_id']}]
        self.settings = replace(self.settings, all_seasons=True)
        args = cli.parse_args(['--url', f"ss{other['season_id']}", '--url', 'ss28541'])
        with patch('builtins.input', side_effect=['1']) as prompt:
            self.assertEqual(application.run_with_settings(args, self.settings, None), 0, self.stderr.getvalue())
        prompt.assert_called_once()
        self.assertEqual(len(self.calls), 6)

    def test_all_seasons_skip_applies_to_entire_group(self) -> None:
        """验证s跳过全部关联季，不为后续季重新询问或启动传输；无返回值。"""
        self.add_related_season()
        self.settings = replace(self.settings, all_seasons=True)
        with patch('builtins.input', side_effect=['s']) as prompt:
            self.assertEqual(application.run_with_settings(cli.parse_args(['--url', 'ss28541']), self.settings, None), 2)
        prompt.assert_called_once()
        self.assertIn('本番剧全部关联季', prompt.call_args.args[0])
        self.assertFalse(self.calls)

    def test_all_seasons_rank_cancel_stops_before_transfer(self) -> None:
        """验证q、EOF和Ctrl+C均向入口传播取消，不继续确认或下载其他季；无返回值。"""
        self.add_related_season()
        self.settings = replace(self.settings, all_seasons=True)
        for response in ('q', EOFError(), KeyboardInterrupt()):
            with self.subTest(response=repr(response)), patch('builtins.input', side_effect=[response]) as prompt:
                with self.assertRaises(KeyboardInterrupt):
                    application.run_with_settings(cli.parse_args(['--url', 'ss28541']), self.settings, None)
                prompt.assert_called_once()
                self.assertFalse(self.calls)

    def test_all_seasons_blocked_season_keeps_other_downloads(self) -> None:
        """验证首季全部无权限时仍只询问一次，其他季继续按所选排名下载；无返回值。"""
        other = self.add_related_season()
        self.blocked.update(episode['id'] for episode in other['episodes'])
        self.settings = replace(self.settings, all_seasons=True)
        with patch('builtins.input', side_effect=['1']) as prompt:
            self.assertEqual(application.run_with_settings(cli.parse_args(['--url', 'ss28541']), self.settings, None), 2)
        prompt.assert_called_once()
        self.assertEqual(set(self.calls), {f'ss28541_p{part}' for part in (1, 2, 3)})
        self.assertTrue(all(item['status'] == 'completed' for item in self.record().data['files']))

    def test_all_seasons_no_available_rank_does_not_prompt(self) -> None:
        """验证全部季均无可下载流时直接报告失败，不弹空档位菜单；无返回值。"""
        self.add_related_season()
        self.blocked.update(episode['id'] for season in self.seasons.values() for episode in season['episodes'])
        self.settings = replace(self.settings, all_seasons=True)
        self.assertEqual(application.run_with_settings(cli.parse_args(['--url', 'ss28541']), self.settings, None), 2)
        self.prompt.assert_not_called()
        self.assertFalse(self.calls)

    def test_all_seasons_rank_preserves_independent_merges_for_both_priorities(self) -> None:
        """验证两种优先策略均只确认一次排名，各季使用自己的实际画质独立拼接，不跨季合并；无返回值。"""
        self.add_related_season()
        self.catalogs.update({episode['id']: (80, 120) for episode in self.season['episodes']})
        args = cli.parse_args(['--url', 'ss28541', '--all-parts', '--merge-parts', '0'])
        for priority in ('quality', 'merge'):
            with self.subTest(priority=priority):
                self.settings = replace(self.settings, all_seasons=True, priority=priority, output_dir=self.root / priority)
                self.concatenate.reset_mock()
                with patch('builtins.input', side_effect=['1']) as prompt:
                    self.assertEqual(application.run_with_settings(args, self.settings, None), 0, self.stderr.getvalue())
                prompt.assert_called_once()
                self.assertEqual(self.concatenate.call_count, 2)
                for season in self.seasons.values():
                    source_id = f"ss{season['season_id']}"
                    record = library.get_video_info(self.settings, source_id)
                    code = 120 if season is self.season else 80
                    self.assertEqual({item['quality_id'] for item in record.data['files']}, {code})
                    result = MergedVideoInfo(self.settings.output_dir, record.quality_directory(
                        entry_quality(record.data['files'][0]), role='merged')).data['files']
                    self.assertEqual(len(result), 1)
                    self.assertEqual((result[0]['source_id'], result[0]['source_parts']), (source_id, [1, 2, 3]))

    def test_all_seasons_merge_priority_respects_each_episode_rank(self) -> None:
        """验证拼接优先只在各集排名所选档位以内找兼容流，不能把某集从第二档提高到第一档；无返回值。"""
        self.add_related_season()
        self.settings = replace(self.settings, all_seasons=True, priority='merge')
        self.catalogs = {episode['id']: (80, 120) for season in self.seasons.values() for episode in season['episodes']}
        self.catalogs.update({episode['id']: codes for episode, codes in zip(
            self.season['episodes'], ((64, 80, 120), (32, 64, 80), (32, 64, 80)))})
        args = cli.parse_args(['--url', 'ss28541', '--all-parts', '--merge-parts', '0'])
        with patch('builtins.input', side_effect=['2']) as prompt:
            self.assertEqual(application.run_with_settings(args, self.settings, None), 0, self.stderr.getvalue())
        prompt.assert_called_once()
        self.assertEqual({item['quality_id'] for item in self.record().data['files']}, {64})
        self.assertEqual(self.concatenate.call_count, 2)

    def test_all_seasons_manifest_keeps_per_season_union_rank(self) -> None:
        """验证清单仍按每季档位并集排名决定上限，不应用普通链接的逐集排名交互；无返回值。"""
        other = self.add_related_season()
        self.settings = replace(self.settings, all_seasons=True)
        self.catalogs = {episode['id']: (64, 80) for episode in other['episodes']}
        self.catalogs[self.season['episodes'][0]['id']] = (80, 120)
        self.assertEqual(self.run_list(quality_rank=2), 0, self.stderr.getvalue())
        self.prompt.assert_not_called()
        self.assertEqual({item['quality_id'] for item in self.record().data['files']}, {80})
        record = library.get_video_info(self.settings, f"ss{other['season_id']}")
        self.assertEqual({item['quality_id'] for item in record.data['files']}, {64})

    def test_single_then_whole_season_share_directory_and_reuse(self) -> None:
        """验证先下载 P2 再补全整季仅传输新增剧集，季身份与每集真实身份独立保存；无返回值。"""
        ep = self.season['episodes'][1]['id']
        self.assertEqual(self.run_list(f'ep{ep}', all_parts=False), 0, self.stderr.getvalue())
        before = self.record().base_directory
        self.assertEqual([item['part'] for item in self.record().data['files']], [2])
        self.assertEqual(self.run_list(), 0, self.stderr.getvalue())
        record = self.record()
        self.assertEqual(record.base_directory, before)
        self.assertEqual(self.calls, {f'ss28541_p{part}': 2 for part in (1, 2, 3)})
        self.assertIsNone(record.data['bvid'])
        self.assertEqual(record.data['schema_version'], 8)
        self.assertEqual(record.data['errors'], [])
        for item in record.data['files']:
            episode = self.season['episodes'][item['part'] - 1]
            self.assertEqual((item['episode_id'], item['source_cid'], item['bvid']),
                             (episode['id'], episode['cid'], episode['bvid']))
            self.assertEqual(item['source_url'], f"https://www.bilibili.com/bangumi/play/ep{episode['id']}")

    def test_direct_duplicate_episode_and_season_inputs_choose_once(self) -> None:
        """验证直接 ep 与 ss 输入合并为同季任务，只询问一次上限且每集只传输一次；无返回值。"""
        args = cli.parse_args(['--url', f"ep{self.season['episodes'][1]['id']}", '--url', 'ss28541', '--all-parts'])
        with patch('builtins.input', return_value='1') as prompt:
            self.assertEqual(application.run_with_settings(args, self.settings, None), 0, self.stderr.getvalue())
        prompt.assert_called_once()
        self.assertEqual(self.calls, {f'ss28541_p{part}': 2 for part in (1, 2, 3)})

    def test_quality_ceiling_group_merge_and_offline_season_merge(self) -> None:
        """验证各集遵守季上限、分组实际档位一致才拼接，ss 离线拼接不访问认证；无返回值。"""
        self.catalogs = {self.season['episodes'][0]['id']: (80, 120), self.season['episodes'][1]['id']: (64, 80)}
        self.assertEqual(self.run_list(merge='2-3'), 0, self.stderr.getvalue())
        self.assertEqual({item['part']: item['quality_id'] for item in self.record().data['files']}, {1: 120, 2: 80, 3: 80})
        args = cli.parse_args(['--merge-video', 'ss28541', '--merge-parts', '2-3'])
        with patch.object(auth, 'ensure_login', side_effect=AssertionError('离线不应登录')), patch('builtins.input', return_value='2'):
            self.assertEqual(application.run_with_settings(args, self.settings, None), 0)
        indexes = [MergedVideoInfo(self.settings.output_dir, path.parent)
                   for path in self.settings.output_dir.rglob('video_info.json') if path.parent.name.endswith('_合并')]
        files = [item for index in indexes for item in index.data['files']]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['source_id'], 'ss28541')
        self.assertIsNone(files[0]['bvid'])
        self.assertEqual([source['episode_id'] for source in files[0]['source_snapshot']],
                         [episode['id'] for episode in self.season['episodes'][1:]])

    def test_merge_priority_chooses_compatible_season_quality(self) -> None:
        """验证拼接优先使用所有所选集共有的兼容档位，并保留原有季画质上限规则；无返回值。"""
        self.settings = replace(self.settings, priority='merge')
        self.catalogs[self.season['episodes'][0]['id']] = (80, 120)
        self.assertEqual(self.run_list(merge='0'), 0, self.stderr.getvalue())
        self.assertEqual({item['quality_id'] for item in self.record().data['files']}, {80})
        self.assertEqual(self.concatenate.call_count, 1)

    def test_unavailable_episode_keeps_other_downloads_and_skips_full_merge(self) -> None:
        """验证一集无权限时其他集继续，整季拼接不使用缺失集，退出码为2；无返回值。"""
        self.blocked.add(self.season['episodes'][1]['id'])
        self.assertEqual(self.run_list(merge='0'), 2)
        self.assertEqual(set(self.calls), {'ss28541_p1', 'ss28541_p3'})
        self.concatenate.assert_not_called()

    def test_episode_identity_changes_even_with_same_cid(self) -> None:
        """验证原站替换同一位置的 EP 时，即使 CID 相同也不复用旧剧集媒体；无返回值。"""
        self.assertEqual(self.run_list(), 0, self.stderr.getvalue())
        previous = self.record().data['files'][1]['episode_id']
        self.season['episodes'][1]['id'] = 888888
        self.assertEqual(self.run_list(), 0, self.stderr.getvalue())
        updated = next(item for item in self.record().data['files'] if item['part'] == 2)
        self.assertNotEqual(previous, updated['episode_id'])
        self.assertEqual(updated['episode_id'], 888888)
        self.assertEqual(self.calls['ss28541_p2'], 4)

    def test_interrupted_stream_resumes_after_layout_change(self) -> None:
        """验证失败断点按 EP 隔离，切换画质布局后仍能找到原断点并补全；无返回值。"""
        self.settings = replace(self.settings, mix_quality=True)
        self.fail_parts.add(2)
        self.assertEqual(self.run_list(), 2)
        self.assertTrue(list(self.settings.output_dir.rglob('*.part')))
        self.fail_parts.clear()
        self.settings = replace(self.settings, mix_quality=False)
        self.assertEqual(self.run_list(), 0, self.stderr.getvalue())
        self.assertTrue(self.resumed)
        self.assertTrue(all(f"ep{self.season['episodes'][1]['id']}" in str(path) for path in self.resumed))
        self.assertEqual(len(self.record().data['files']), 3)

    def test_metadata_refresh_preserves_bangumi_identity(self) -> None:
        """验证扫描刷新能发现番剧源索引，按季接口更新信息且没有缺失 UP 主错误；无返回值。"""
        self.assertEqual(self.run_list(), 0, self.stderr.getvalue())
        self.season['title'] = '更新后的番剧标题'
        self.assertEqual(application.run_with_settings(cli.parse_args(['--refresh-metadata']), self.settings, None), 0,
                         self.stderr.getvalue())
        self.assertEqual(self.record().data['title'], self.season['title'])
        self.assertEqual(self.record().data['errors'], [])

    def test_format_listing_uses_same_bangumi_adapter_without_downloading(self) -> None:
        """验证画质查询复用 PGC 适配与真实字幕身份，不执行媒体传输或询问上限；无返回值。"""
        args = cli.parse_args(['--url', 'ss28541', '--list-formats'])
        self.assertEqual(application.run_with_settings(args, self.settings, None), 0, self.stderr.getvalue())
        self.assertFalse(self.calls)
        self.prompt.assert_not_called()


class SourceIdentityMigrationTests(unittest.TestCase):
    """验证旧普通投稿和合并索引按需升级身份，不改变真实 BV 号和现有条目标识。"""

    def test_unrelated_information_file_does_not_block_source_scanning(self) -> None:
        """验证下载根目录含其他工具的信息文件时，投稿查找和源扫描仍忽略无关文件；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(config.load_settings(CONFIG_PATH), output_dir=Path(directory).resolve())
            unrelated = settings.output_dir / 'other_tool' / 'video_info.json'
            unrelated.parent.mkdir()
            unrelated.write_text(json.dumps({'title': '其他来源'}), encoding='utf-8')
            bvid = 'BV1xx411c7mD'
            record = library.get_video_info(settings, bvid, '投稿', 1)
            record.save()
            self.assertEqual(library.get_video_info(settings, bvid).data['source_id'], bvid)
            self.assertEqual(metadata.discover_videos(settings.output_dir), ([bvid], []))

    def test_invalid_identity_in_own_index_is_reported(self) -> None:
        """验证来源自身索引身份缺失时仍明确失败，不把损坏的自身记录当成无关文件忽略；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(config.load_settings(CONFIG_PATH), output_dir=Path(directory).resolve())
            folder = settings.output_dir / '投稿_BV1xx411c7mD_分P'
            folder.mkdir()
            (folder / 'video_info.json').write_text(json.dumps({'schema_version': 8, 'title': '损坏记录'}), encoding='utf-8')
            with self.assertRaises(errors.ToolError):
                library.get_video_info(settings, 'BV1xx411c7mD')

    def test_v7_source_and_v1_merged_indexes_upgrade(self) -> None:
        """从真实临时索引移除新字段模拟历史格式，重新打开后校验身份和版本；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(config.load_settings(CONFIG_PATH), output_dir=Path(directory).resolve())
            bvid = 'BV1xx411c7mD'
            record = library.get_video_info(settings, bvid, '旧普通投稿', 1)
            record.save()
            data = json.loads(record.path.read_text(encoding='utf-8'))
            data['schema_version'] = 7
            for key in ('source_id', 'source_type'):
                data.pop(key)
            record.path.write_text(json.dumps(data), encoding='utf-8')
            upgraded = library.get_video_info(settings, bvid)
            self.assertEqual((upgraded.data['schema_version'], upgraded.data['source_id'], upgraded.data['bvid']), (8, bvid, bvid))
            folder = settings.output_dir / '旧合并_合并'
            folder.mkdir()
            merged_path = folder / 'video_info.json'
            merged_path.write_text(json.dumps({'schema_version': 1, 'record_type': 'merged', 'files': [
                {'id': bvid + ':merged:qn80', 'kind': 'merged', 'bvid': bvid, 'errors': []}], 'merge_results': []}), encoding='utf-8')
            merged = MergedVideoInfo(settings.output_dir, folder)
            merged.save()
            self.assertEqual(merged.data['schema_version'], 2)
            self.assertEqual(merged.data['files'][0]['source_id'], bvid)
            self.assertEqual(merged.data['files'][0]['bvid'], bvid)
