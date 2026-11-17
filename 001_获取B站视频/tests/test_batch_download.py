"""离线验证 YAML 清单、共享线程调度、独立认证会话和并发记录更新。"""

from collections import Counter
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from threading import Barrier, Event, Lock, get_ident
import subprocess
import json
import re
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import yaml

from bilibili_downloader import api, auth, config, errors, library, manifest, media, metadata, scheduler, storage
from bilibili_downloader import downloader as transfer
from bilibili_downloader import subtitles
from bilibili_downloader.merged_records import MergedVideoInfo
from bilibili_downloader.models import Quality, VideoTarget
import download_bilibili as cli
from bilibili_downloader import runtime
from tests import CONFIG_PATH, fake_auth_snapshot


class ManifestTests(unittest.TestCase):
    """验证输入展开和静态配置校验，不创建登录会话或真实下载。"""

    def setUp(self) -> None:
        """创建清单临时目录及配置；无参数和返回值。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = replace(config.load_settings(CONFIG_PATH), output_dir=self.root, threads=3)

    def write(self, data: object, name: str = 'list.yaml') -> Path:
        """将 data 写入临时 name 清单，返回其绝对路径。"""
        path = self.root / name
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
        return path

    def test_defaults_overrides_and_false_are_preserved(self) -> None:
        """验证逐项覆盖全局及命令行，false 不被默认 true 覆盖，标题仅作为备注读取；无返回值。"""
        path = self.write({'date': '备注', 'all-parts': True, 'merge-parts': 0, 'quality': 1, 'download_list': [
            {'title': '备注', 'url': 'BV1xx411c7mD'},
            {'url': 'BV1yy411c7mD', 'all-parts': False, 'merge-parts': '1,1;2,2', 'quality': 2},
        ]})
        requests = manifest.expand_requests([str(path)], self.settings, True, '-1')
        self.assertEqual([(value.all_parts, value.merge_parts, value.quality_rank) for value in requests],
                         [(True, '0', 1), (False, '1,1;2,2', 2)])
        self.assertEqual(requests[0].title, '备注')
        self.assertEqual(self.settings.threads, 3)

    def test_mixed_inputs_relative_paths_and_yaml_extension(self) -> None:
        """验证链接和多个清单混用时按输入顺序展开，相对路径以当前目录定位；无返回值。"""
        path = self.write({'quality': 1, 'download_list': [{'url': 'BV1yy411c7mD'}]}, 'list.YML')
        with patch.object(Path, 'cwd', return_value=self.root):
            values = manifest.expand_requests(['BV1xx411c7mD', path.name, 'av123', str(path)], self.settings, False, '-1')
        self.assertEqual([value.url for value in values], [api.normalize_url(value) for value in
                         ('BV1xx411c7mD', 'BV1yy411c7mD', 'av123', 'BV1yy411c7mD')])
        self.assertFalse(manifest.is_manifest('https://b23.tv/example.yaml'))
        self.assertIsNone(values[0].quality_rank)

    def test_optional_commit_note_does_not_change_requests(self) -> None:
        """验证全局 commit 备注为空、填写或省略时均不改变下载请求；无参数和返回值。"""
        data = {'quality': 1, 'download_list': [{'url': 'BV1xx411c7mD'}]}
        path = self.write(data)
        expected = manifest.expand_requests([str(path)], self.settings, False, '-1')
        for note in ('', '下载备注'):
            with self.subTest(note=note):
                self.write({**data, 'commit': note})
                self.assertEqual(manifest.expand_requests([str(path)], self.settings, False, '-1'), expected)

    def test_invalid_manifest_fails_before_authentication_and_ffmpeg(self) -> None:
        """验证清单错误在联网和 FFmpeg 定位前返回 1，并报告文件及任务序号；无返回值。"""
        path = self.write({'quality': 1, 'download_list': [{'url': 'BV1xx411c7mD'}, {'url': 'BV1yy411c7mD', 'quality': False}]})
        report = StringIO()
        with patch.object(config, 'load_settings', return_value=self.settings), patch.object(auth, 'authenticated_session') as login, patch.object(media, 'resolve_ffmpeg') as ffmpeg, redirect_stderr(report):
            self.assertEqual(cli.main(['--url', str(path)]), 1)
        login.assert_not_called()
        ffmpeg.assert_not_called()
        self.assertIn('第 2 项', report.getvalue())
        self.assertIn(str(path), report.getvalue())

    def test_missing_bad_and_unknown_values_are_rejected(self) -> None:
        """验证必填值、类型、未知字段及空列表均被拒绝，不接受将布尔值用作整数；无返回值。"""
        cases = [None, [], {'download_list': []}, {'download_list': [{'url': 'BV1xx411c7mD'}]},
                 {'quality': 1, 'all-parts': 'true', 'download_list': [{'url': 'BV1xx411c7mD'}]},
                 {'quality': 1, 'merge-parts': True, 'download_list': [{'url': 'BV1xx411c7mD'}]},
                 {'quality': 1, 'unknown': 0, 'download_list': [{'url': 'BV1xx411c7mD'}]},
                 {'quality': 1, 'download_list': [{'url': 'BV1xx411c7mD', 'title': None}]},
                 {'quality': 1, 'download_list': [{'url': 'list.yaml'}]},
                 {'quality': 1, 'download_list': [{'url': 'https://www.bilibili.com:bad/video/BV1xx411c7mD'}]}]
        for index, data in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(errors.ToolError):
                manifest.expand_requests([str(self.write(data))], self.settings, False, '-1')

    def test_threads_must_be_positive_integer(self) -> None:
        """验证线程数由 YAML 配置读取，拒绝 0、负数、浮点数、字符串和布尔值；无返回值。"""
        original = CONFIG_PATH.read_text(encoding='utf-8')
        path = self.root / 'config.yaml'
        for value in ('0', '-1', '1.5', '"3"', 'true'):
            path.write_text(re.sub(r'(?m)^  threads:.*$', '  threads: ' + value, original), encoding='utf-8')
            with self.subTest(value=value), self.assertRaisesRegex(errors.ToolError, 'threads'):
                config.load_settings(path)

    def test_cli_accepts_manifest_merge_without_all_parts(self) -> None:
        """验证清单可提供分P设置，无需额外 --all-parts，普通链接仍校验原有互斥关系；无返回值。"""
        args = cli.parse_args(['--url', 'download_list/list.yaml', '--merge-parts', '0'])
        self.assertFalse(args.all_parts)
        self.assertEqual(args.merge_parts, '0')
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            cli.parse_args(['--url', 'BV1xx411c7mD', '--merge-parts', '0'])


class SchedulerTests(unittest.TestCase):
    """用屏障和取消事件验证真实线程池，不依赖下载器实现。"""

    def test_global_limit_fifo_and_main_thread_callbacks(self) -> None:
        """验证首次填满 A 的两个分P和 B-P1、总并发不超限，准备与完成回调在主线程执行；无返回值。"""
        main, barrier, guard = get_ident(), Barrier(3), Lock()
        jobs = ['A1', 'A2', 'B1', 'B2', 'B3']
        dispatched, results, started = [], [], []
        active = maximum = 0

        def prepare(job: str) -> str:
            """在主线程记录 job 派发顺序并返回负载。"""
            self.assertEqual(get_ident(), main)
            dispatched.append(job)
            return job

        def execute(job: str, event: Event) -> str:
            """并发处理 job，event 为取消标志，首批任务等待屏障，返回自身标识。"""
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
                started.append(job)
            if job in ('A1', 'A2', 'B1'):
                barrier.wait(timeout=5)
            time.sleep(0.01)
            with guard:
                active -= 1
            return job

        def complete(job: str, result: str | None, error: BaseException | None) -> None:
            """在主线程核对 job 的 result/error，并记录成功结果，无返回值。"""
            self.assertEqual(get_ident(), main)
            self.assertIsNone(error)
            results.append(result)

        scheduler.run_jobs(jobs, 3, prepare, execute, complete, Event())
        self.assertEqual(dispatched, jobs)
        self.assertEqual(set(started[:3]), {'A1', 'A2', 'B1'})
        self.assertEqual(maximum, 3)
        self.assertCountEqual(results, jobs)

    def test_serial_and_failure_continue(self) -> None:
        """验证单线程保持顺序，单项异常不阻止其他任务；无返回值。"""
        results = []

        def execute(job: int, event: Event) -> int:
            """处理 job 并在第二项模拟失败；event 为取消标志，成功返回编号。"""
            if job == 2:
                raise errors.ToolError('failed')
            return job

        scheduler.run_jobs([1, 2, 3], 1, lambda job: job, execute,
                           lambda job, result, error: results.append((job, result, error)), Event())
        self.assertEqual([value[0] for value in results], [1, 2, 3])
        self.assertIsInstance(results[1][2], errors.ToolError)
        self.assertEqual(results[2][1], 3)

    def test_cancel_stops_dispatch_and_notifies_running_task(self) -> None:
        """验证主线程取消后通知运行任务退出，后续队列不启动；无返回值。"""
        started, event, calls = Event(), Event(), []

        def prepare(job: int) -> int:
            """准备 job；第二项等待首项启动后模拟 Ctrl+C，正常返回编号。"""
            if job == 2:
                self.assertTrue(started.wait(5))
                raise KeyboardInterrupt
            return job

        def execute(job: int, cancel: Event) -> int:
            """标记 job 已启动并等待 cancel，取消后协作退出，不产生正式结果。"""
            calls.append(job)
            started.set()
            self.assertTrue(cancel.wait(5))
            errors.check_cancel(cancel)
            return job

        with self.assertRaises(KeyboardInterrupt):
            scheduler.run_jobs([1, 2, 3, 4], 3, prepare, execute, lambda *values: None, event)
        self.assertEqual(calls, [1])
        self.assertTrue(event.is_set())


class FakeDownloader:
    """线程归属严格的离线选流/传输器，帮助发现错误复用主线程下载器。"""

    def __init__(self, owner: 'BatchEngineTests') -> None:
        """保存测试 owner，并记录创建线程；无返回值。"""
        self.owner, self.thread = owner, get_ident()
        self.params, self.hooks = {}, []

    def __enter__(self) -> 'FakeDownloader':
        """返回当前下载器；无参数。"""
        return self

    def __exit__(self, *values: object) -> None:
        """接收上下文退出 values，不持有外部资源；无返回值。"""

    def extract_info(self, url: str, **options: object) -> dict:
        """在创建线程查询 url；options 为兼容参数，返回测试原始格式。"""
        self.owner.assertEqual(get_ident(), self.thread)
        bvid = api.VIDEO_PATH.fullmatch(urlsplit(url).path)['id']
        part = int(url.rsplit('p=', 1)[1])
        identity = f'{bvid}_p{part}'
        self.owner.extracts[identity] += 1
        codes = self.owner.catalogs.get(identity, (80,))
        if self.owner.dynamic and self.owner.extracts[identity] > 1:
            codes = (80,)
        return {'id': identity, 'title': f'分P{part}', 'formats': [
            {'format_id': str(code), 'quality': code, 'format': f'画质{code}', 'url': 'https://example.com/video',
             'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'none', 'width': 64, 'height': 64, 'fps': 5}
            for code in codes]}

    def process_ie_result(self, info: dict, **options: object) -> dict:
        """在创建线程对 info 选择一个已限制画质的格式；options 为兼容参数，返回选流结果。"""
        self.owner.assertEqual(get_ident(), self.thread)
        return {**info, 'requested_formats': [info['formats'][0]]}

    def list_formats(self, info: dict) -> None:
        """忽略 info 的格式显示；无返回值。"""

    def add_progress_hook(self, hook: object) -> None:
        """保存 hook，传输时调用以支持取消；无返回值。"""
        self.hooks.append(hook)

    def dl(self, filename: str, info: dict) -> tuple[bool, bool]:
        """在创建线程传输 info 到 filename，并把进度交给 hooks，返回成功标记。"""
        self.owner.assertEqual(get_ident(), self.thread)
        return self.owner.transfer(filename, info, self.hooks)


class BatchEngineTests(unittest.TestCase):
    """走真实清单、调度、记录和封装协调流程，仅替换远程接口与媒体传输。"""

    def setUp(self) -> None:
        """创建隔离目录和离线接口，保留实际线程及JSON读写；无参数和返回值。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = replace(config.load_settings(CONFIG_PATH), output_dir=self.root / 'downloads',
                                priority='quality')
        self.ids = ['BV1xx411c7mD', 'BV1yy411c7mD']
        self.counts = dict.fromkeys(self.ids, 1)
        self.catalogs, self.extracts, self.calls = {}, Counter(), Counter()
        self.main, self.guard, self.started = get_ident(), Lock(), Event()
        self.active = self.maximum = self.snapshots = 0
        self.barrier, self.dynamic, self.cancel, self.failed = None, False, False, set()
        self.sessions = []
        self.stdout = self.enterContext(redirect_stdout(StringIO()))
        self.stderr = self.enterContext(redirect_stderr(StringIO()))
        self.enterContext(patch.object(config, 'load_settings', return_value=self.settings))
        self.enterContext(patch.object(media, 'resolve_ffmpeg', return_value='ffmpeg'))
        self.enterContext(patch.object(auth, 'authenticated_session', return_value=MagicMock()))
        self.enterContext(patch.object(auth, 'ensure_login', side_effect=self.ensure_login))
        self.enterContext(patch.object(auth, 'snapshot_session', side_effect=self.snapshot))
        self.enterContext(patch.object(transfer, 'make_downloader', side_effect=self.make_downloader))
        self.enterContext(patch.object(api, 'resolve_targets', side_effect=self.resolve_targets))
        self.enterContext(patch.object(api, 'api_json', side_effect=self.remote_metadata))
        self.enterContext(patch.object(subtitles, 'list_subtitles', return_value=[]))
        self.enterContext(patch.object(media, 'mux_streams', side_effect=self.mux))
        self.enterContext(patch.object(metadata, 'probe_media', return_value={'duration_seconds': 2.0, 'width': 64,
                          'height': 64, 'fps': 5, 'video_codec': 'h264', 'audio_codec': None, 'errors': []}))
        self.enterContext(patch.object(runtime, 'run_process', side_effect=self.concatenate))
        self.input = self.enterContext(patch('builtins.input', side_effect=AssertionError('清单不应读取终端')))

    def ensure_login(self, context: object, settings: object) -> dict:
        """原浏览器 context 限定主线程，处理池只使用独立 HTTP 会话；settings 为配置，返回离线账户。"""
        if get_ident() != self.main:
            self.assertIsInstance(context, auth.AuthenticatedHttpSession)
        return {'uname': 'test'}

    def snapshot(self, context: object) -> auth.AuthSnapshot:
        """浏览器 context 在主线程提取，独立 HTTP 会话在线程内提取；返回假凭据。"""
        if get_ident() != self.main:
            self.assertIsInstance(context, auth.AuthenticatedHttpSession)
        with self.guard:
            self.snapshots += 1
        return fake_auth_snapshot()

    def make_downloader(self, context: object, settings: object) -> FakeDownloader:
        """为 context/settings 创建独立下载器；工作线程的认证对象必须为 HTTP 会话，返回离线下载器。"""
        if get_ident() != self.main:
            self.assertIsInstance(context, auth.AuthenticatedHttpSession)
            with self.guard:
                self.sessions.append(context)
        return FakeDownloader(self)

    def resolve_targets(self, context: object, url: str, settings: object, all_parts: bool) -> list[VideoTarget]:
        """从 url 解析 BV，并按 all_parts 返回隔离测试中的分P；context/settings 为接口兼容参数。"""
        bvid = next(value for value in self.ids if value in url)
        total = self.counts[bvid]
        return [VideoTarget(f'https://www.bilibili.com/video/{bvid}?p={part}', part, part, total)
                for part in (range(1, total + 1) if all_parts else [1])]

    def remote_metadata(self, context: object, path: str, settings: object, params: dict) -> dict:
        """按 params 返回原站标题和UP主信息；context/path/settings 为兼容参数，返回响应对象。"""
        bvid = params['bvid']
        return {'data': {'bvid': bvid, 'title': '原站标题', 'owner': {'mid': 1, 'name': 'test'},
                         'pages': [{'page': part, 'cid': part} for part in range(1, self.counts[bvid] + 1)]}}

    def transfer(self, filename: str, info: dict, hooks: list) -> tuple[bool, bool]:
        """传输 info 至 filename，hooks 支持中止；屏障验证并发，指定失败时保留部分文件，返回成功标记。"""
        with self.guard:
            self.calls[info['id']] += 1
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            self.started.set()
            if self.cancel or info['id'] in self.failed:
                Path(filename + '.part').write_bytes(b'partial')
                if self.cancel:
                    runtime.current().cancel()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        for hook in hooks:
                            hook({'status': 'downloading'})
                        time.sleep(0.01)
                    self.fail('未收到取消请求')
                raise errors.ToolError('模拟网络失败')
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            Path(filename).write_bytes(info['id'].encode())
            return True, True
        finally:
            with self.guard:
                self.active -= 1

    def mux(self, executable: str, sources: list[Path], formats: list[dict], folder: Path, stem: str) -> Path:
        """模拟 executable 将 sources/formats 封装为 folder/stem，返回成品路径。"""
        path = folder / f'{stem}.mp4'
        path.write_bytes(sources[0].read_bytes())
        return path

    def concatenate(self, command: list[str], **options: object) -> subprocess.CompletedProcess:
        """模拟 command 的FFmpeg输出，options 为兼容参数，返回成功结果。"""
        Path(command[-1]).write_bytes(b'merged')
        return subprocess.CompletedProcess(command, 0, '', '')

    def run_list(self, entries: list[dict], *, options: dict | None = None, extra: list[str] | None = None) -> int:
        """将 entries 和全局 options 写入清单，extra 追加CLI选项，返回真实入口退出码。"""
        path = self.root / 'download_list.yaml'
        data = {'all-parts': True, 'merge-parts': -1, 'quality': 1, **(options or {}), 'download_list': entries}
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
        return cli.main(['--url', str(path), *(extra or [])])

    def test_fill_spare_thread_with_next_post_and_keep_records(self) -> None:
        """验证A两个分P和B一个分P同时下载，各任务使用独立会话且完成记录不会丢失；无返回值。"""
        self.counts[self.ids[0]] = 2
        self.barrier = Barrier(3)
        self.assertEqual(self.run_list([{'url': bvid} for bvid in self.ids]), 0)
        self.assertEqual(self.maximum, 3)
        # 目录画质预查询、逐项准备及字幕处理均使用处理池内的独立会话。
        self.assertEqual(len({id(value) for value in self.sessions}), sum(self.counts.values()) * 2 + len(self.ids))
        for bvid in self.ids:
            record = library.get_video_info(self.settings, bvid)
            self.assertEqual(len(record.data['files']), self.counts[bvid])
            self.assertTrue(all(item['status'] == 'completed' for item in record.data['files']))
        self.input.assert_not_called()

    def test_per_item_settings_override_global_and_cli(self) -> None:
        """验证 false、画质排名及合并禁用覆盖CLI默认值，日志备注不覆盖原站命名；无返回值。"""
        self.counts = dict.fromkeys(self.ids, 2)
        self.catalogs = {f'{bvid}_p{part}': (80, 120) for bvid in self.ids for part in (1, 2)}
        self.assertEqual(self.run_list([{'url': self.ids[0], 'title': '只作备注', 'all-parts': False},
                                      {'url': self.ids[1], 'quality': 2}], extra=['--all-parts', '--merge-parts', '0']), 0,
                         self.stderr.getvalue() + self.stdout.getvalue())
        first, second = [library.get_video_info(self.settings, bvid) for bvid in self.ids]
        self.assertEqual([(item['part'], item['quality_id']) for item in first.data['files']], [(1, 120)])
        self.assertEqual({item['quality_id'] for item in second.data['files']}, {80})
        self.assertNotIn('只作备注', first.path.parent.name)
        self.assertEqual(list(self.settings.output_dir.glob('*_合并')), [])

    def test_manifest_rank_selects_one_union_ceiling_for_all_parts(self) -> None:
        """验证quality排名基于投稿档位并集，不再把第二P各自的第二高误当成统一上限；无返回值。"""
        bvid = self.ids[0]
        self.counts[bvid] = 2
        self.catalogs = {f'{bvid}_p1': (80, 120), f'{bvid}_p2': (64, 80)}
        self.assertEqual(self.run_list([{'url': bvid, 'quality': 2}]), 0)
        record = metadata.open_video(self.settings.output_dir, bvid)
        self.assertEqual({item['quality_id'] for item in record.data['files']}, {80})
        task = next(row for row in self.read_log()['runs'][-1]['entries'] if row['kind'] == '任务')
        self.assertIn('qn=80', task['quality'])
        self.input.assert_not_called()

    def test_new_higher_format_during_refresh_does_not_raise_ceiling(self) -> None:
        """验证两种存放模式在派发前出现更高档位时仍使用最初确定的上限，不重新按排名取高档；无返回值。"""
        original = FakeDownloader.extract_info

        def changing(downloader: FakeDownloader, url: str, **options: object) -> dict:
            """通过原解析器读取 url/options，第二次起加入更高档位，返回可下载格式用于模拟原站变化。"""
            info = original(downloader, url, **options)
            if self.extracts[info['id']] > 1:
                info['formats'].append({**info['formats'][0], 'format_id': '120', 'quality': 120, 'format': '画质120'})
            return info

        for bvid, mixed in zip(self.ids, (False, True)):
            with self.subTest(mix_quality=mixed), patch.object(FakeDownloader, 'extract_info', new=changing), \
                    patch.object(config, 'load_settings', return_value=replace(self.settings, mix_quality=mixed)):
                self.assertEqual(self.run_list([{'url': bvid}]), 0)
            record = metadata.open_video(self.settings.output_dir, bvid)
            self.assertEqual({item['quality_id'] for item in record.data['files']}, {80})
            self.assertIs(record.mix_quality, mixed)

    def test_part_with_no_format_under_ceiling_fails_without_blocking_others(self) -> None:
        """验证只有高于上限档位的P失败，上限内的其他P仍下载完成并记录准确原因；无返回值。"""
        bvid = self.ids[0]
        self.counts[bvid] = 2
        self.catalogs = {f'{bvid}_p1': (80,), f'{bvid}_p2': (120,)}
        self.assertEqual(self.run_list([{'url': bvid, 'quality': 2}]), 2)
        self.assertEqual(self.calls, {f'{bvid}_p1': 1})
        row = next(row for row in self.read_log()['runs'][-1]['entries'] if row['part'] == 'P2')
        self.assertEqual(row['status'], 'failed')
        self.assertIn('上限', row['detail'])

    def test_duplicate_jobs_transfer_once(self) -> None:
        """验证同BV、分P及画质的重复清单项只发生一次实际传输，结果供两项复用；无返回值。"""
        self.assertEqual(self.run_list([{'url': self.ids[0]}, {'url': self.ids[0]}]), 0)
        self.assertEqual(self.calls, {self.ids[0] + '_p1': 1})

    def test_manifest_and_direct_link_use_same_pool(self) -> None:
        """验证普通链接与清单混用时只为普通链接询问画质，两个来源均完成；无返回值。"""
        with patch('builtins.input', return_value='1') as prompt:
            self.assertEqual(self.run_list([{'url': self.ids[0]}], extra=['--url', self.ids[1]]), 0)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(set(self.calls), {bvid + '_p1' for bvid in self.ids})

    def test_global_full_merge_uses_task_quality_without_prompt(self) -> None:
        """验证清单全局 merge-parts:0 自动合并全部分P，并写入当前画质的独立索引；无返回值。"""
        self.counts[self.ids[0]] = 2
        self.assertEqual(self.run_list([{'url': self.ids[0]}], options={'merge-parts': 0}), 0)
        source = library.get_video_info(self.settings, self.ids[0])
        index = MergedVideoInfo(self.settings.output_dir, source.quality_directory(Quality(80, '画质80'), role='merged'))
        self.assertEqual([item['source_parts'] for item in index.data['files']], [[1, 2]])
        self.assertTrue(index.resolve_file(index.data['files'][0]).is_file())
        self.assertTrue(all(item['kind'] == 'part' for item in source.data['files']))
        self.input.assert_not_called()

    def test_rerun_manifest_reuses_downloads_and_overwrites_existing_merge(self) -> None:
        """验证同一清单重复运行时复用已下载分 P，重新覆盖合并成品，且合并索引只有一条记录；无返回值。"""
        self.counts[self.ids[0]] = 2
        self.enterContext(patch.object(config, 'load_settings', return_value=replace(self.settings, overwrite_merged=True)))
        entries = [{'url': self.ids[0]}]
        self.assertEqual(self.run_list(entries, options={'merge-parts': 0}), 0)
        source = library.get_video_info(self.settings, self.ids[0])
        folder = source.quality_directory(Quality(80, '画质80'), role='merged')
        index = MergedVideoInfo(self.settings.output_dir, folder)
        destination = index.resolve_file(index.data['files'][0])
        destination.write_bytes(b'previous-merge')
        self.assertEqual(self.run_list(entries, options={'merge-parts': 0}), 0)
        self.assertEqual(destination.read_bytes(), b'merged')
        self.assertEqual(len(MergedVideoInfo(self.settings.output_dir, folder).data['files']), 1)
        self.assertEqual(self.calls, {self.ids[0] + '_p1': 1, self.ids[0] + '_p2': 1})

    def test_rerun_manifest_with_overwrite_disabled_keeps_merge_and_logs_reuse(self) -> None:
        """验证关闭覆盖时不调用 FFmpeg，保留旧媒体及成品属性，追加复用结果并返回成功；无返回值。"""
        self.counts[self.ids[0]] = 2
        entries = [{'url': self.ids[0]}]
        self.assertEqual(self.run_list(entries, options={'merge-parts': 0}), 0)
        source = library.get_video_info(self.settings, self.ids[0])
        folder = source.quality_directory(Quality(80, '画质80'), role='merged')
        index = MergedVideoInfo(self.settings.output_dir, folder)
        destination = index.resolve_file(index.data['files'][0])
        content, indexed_files = destination.read_bytes(), deepcopy(index.data['files'])
        settings = replace(self.settings, overwrite_merged=False)
        with patch.object(config, 'load_settings', return_value=settings), patch.object(runtime, 'run_process') as concatenate:
            self.assertEqual(self.run_list(entries, options={'merge-parts': 0}), 0)
        concatenate.assert_not_called()
        self.assertEqual(destination.read_bytes(), content)
        self.assertEqual(MergedVideoInfo(self.settings.output_dir, folder).data['files'], indexed_files)
        self.assertEqual([item['status'] for item in MergedVideoInfo.for_results(source).data['merge_results']], ['completed', 'reused'])
        rows = [entry for entry in self.read_log()['runs'][-1]['entries'] if entry['kind'] == '合并']
        self.assertEqual([entry['status'] for entry in rows], ['reused'])
        self.assertEqual(self.calls, {self.ids[0] + '_p1': 1, self.ids[0] + '_p2': 1})

    def read_log(self) -> dict:
        """读取当前隔离 output_dir 的 JSON 历史；返回日志对象，供状态时序断言使用。"""
        return metadata.read_json(self.settings.output_dir / self.settings.log_state_filename)

    def test_ready_post_merges_while_other_downloads_keep_dispatching(self) -> None:
        """验证 A 下载完即合并且合并期间仍派发 B-P2，HTML 在进程结束前显示合并中；无返回值。"""
        first_started, merging, next_started = Event(), Event(), Event()
        self.counts = {self.ids[0]: 2, self.ids[1]: 3}
        original_transfer, original_concat = self.transfer, self.concatenate
        order = []

        def transfer(filename: str, info: dict, hooks: list) -> tuple[bool, bool]:
            """按 info 阻塞首个 B 分 P 并观察后续派发；filename/hooks 交原传输器，返回传输结果。"""
            if info['id'].startswith(self.ids[0]):
                self.assertTrue(first_started.wait(5))
            elif info['id'].endswith('_p1'):
                first_started.set()
                self.assertTrue(merging.wait(5), 'A 必须在 B 下载结束前启动合并')
            else:
                next_started.set()
            return original_transfer(filename, info, hooks)

        def concatenate(command: list[str], **options: object) -> subprocess.CompletedProcess:
            """由 command 的播放列表识别投稿并阻塞 A 合并以验证补位；options 透传，返回 FFmpeg 结果。"""
            playlist = Path(command[command.index('-i') + 1]).read_text(encoding='utf-8')
            bvid = next(bvid for bvid in self.ids if bvid in playlist)
            order.append(bvid)
            if bvid == self.ids[0]:
                entries = self.read_log()['runs'][-1]['entries']
                self.assertTrue(any(row['kind'] == '合并' and row['status'] == 'merging' for row in entries))
                merging.set()
                self.assertTrue(next_started.wait(5), '合并不得阻塞下载池继续派发')
            return original_concat(command, **options)

        self.transfer = transfer
        with patch.object(runtime, 'run_process', side_effect=concatenate):
            self.assertEqual(self.run_list([{'url': bvid} for bvid in self.ids], options={'merge-parts': 0}), 0,
                             self.stderr.getvalue() + self.stdout.getvalue())
        self.assertEqual(order, self.ids)
        self.assertTrue(all(row['status'] == 'completed' for row in self.read_log()['runs'][-1]['entries']))

    def test_later_post_finishes_first_and_merges_first(self) -> None:
        """验证后输入的 B 提前完成时先合并 B，A 未结束不会阻塞；无返回值。"""
        merged = Event()
        self.counts = {self.ids[0]: 3, self.ids[1]: 2}
        original_transfer, original_concat = self.transfer, self.concatenate
        order = []

        def transfer(filename: str, info: dict, hooks: list) -> tuple[bool, bool]:
            """阻塞 A 的前两 P，第三 P 让出线程给 B；参数透传原传输器，返回结果。"""
            if info['id'] in (self.ids[0] + '_p1', self.ids[0] + '_p2'):
                self.assertTrue(merged.wait(5), '先完成的 B 应独立合并')
            return original_transfer(filename, info, hooks)

        def concatenate(command: list[str], **options: object) -> subprocess.CompletedProcess:
            """记录 command 对应投稿的合并顺序，B 合并完成后放行 A；options 透传，返回结果。"""
            playlist = Path(command[command.index('-i') + 1]).read_text(encoding='utf-8')
            bvid = next(bvid for bvid in self.ids if bvid in playlist)
            order.append(bvid)
            result = original_concat(command, **options)
            if bvid == self.ids[1]:
                merged.set()
            return result

        self.transfer = transfer
        with patch.object(runtime, 'run_process', side_effect=concatenate):
            self.assertEqual(self.run_list([{'url': bvid} for bvid in self.ids], options={'merge-parts': 0}), 0)
        self.assertEqual(order, list(reversed(self.ids)))

    def test_html_keeps_history_and_failure_group_paths(self) -> None:
        """验证日志保留历次运行、重复传输路径及分组成功失败明细；无返回值。"""
        self.counts[self.ids[0]] = 2
        self.assertEqual(self.run_list([{'url': self.ids[0], 'merge-parts': '1,1;1,3'}]), 2)
        entries = self.read_log()['runs'][0]['entries']
        groups = [row for row in entries if row['kind'] == '合并']
        self.assertEqual([row['status'] for row in groups], ['completed', 'failed'])
        self.assertTrue(Path(groups[0]['path']).is_file())
        self.assertIn('未分配画质', groups[1]['detail'])
        self.assertEqual(self.run_list([{'url': self.ids[0]}, {'url': self.ids[0]}]), 0)
        data = self.read_log()
        self.assertEqual(len(data['runs']), 2)
        downloads = [row for row in data['runs'][1]['entries'] if row['kind'] == '下载']
        self.assertTrue(all(row['status'] == 'reused' and Path(row['path']).is_file() for row in downloads))

    def test_offline_group_merge_is_logged_without_authentication(self) -> None:
        """验证离线拼接也保留每组成功和缺失分 P 错误，并继续使用同一历史日志；无返回值。"""
        self.counts[self.ids[0]] = 2
        self.assertEqual(self.run_list([{'url': self.ids[0]}]), 0)
        with patch.object(auth, 'authenticated_session', side_effect=AssertionError('离线合并不应登录')):
            self.assertEqual(cli.main(['--merge-video', self.ids[0], '--merge-parts', '2,1;3,3']), 2)
        run = self.read_log()['runs'][-1]
        self.assertEqual(run['label'], '离线拼接')
        self.assertEqual([row['status'] for row in run['entries']], ['completed', 'failed'])
        self.assertIn('当前投稿列表', run['entries'][1]['detail'])

    def test_persistent_log_failure_does_not_fail_download_or_merge(self) -> None:
        """验证只有日志 JSON 替换持续失败时，真实下载记录及合并成品仍成功且退出码为 0；无返回值。"""
        self.counts[self.ids[0]] = 2
        original = Path.replace

        def replace_file(source: Path, destination: Path) -> Path:
            """仅拒绝 source 到日志 destination 的替换，其他媒体及记录执行真实替换并返回 Path。"""
            if Path(destination).name == self.settings.log_state_filename:
                raise OSError('模拟日志磁盘不可写')
            return original(source, destination)

        with patch.object(Path, 'replace', replace_file):
            self.assertEqual(self.run_list([{'url': self.ids[0]}], options={'merge-parts': 0}), 0)
        source = library.get_video_info(self.settings, self.ids[0])
        self.assertTrue(all(entry['status'] == 'completed' for entry in source.data['files']))
        index = MergedVideoInfo(self.settings.output_dir, source.quality_directory(Quality(80, '画质80'), role='merged'))
        self.assertTrue(index.resolve_file(index.data['files'][0]).is_file())

    def test_quality_changes_follow_rank_without_prompt(self) -> None:
        """验证派发前最高档消失时，清单按实际排名重新选画质且不进行交互；无返回值。"""
        self.catalogs[self.ids[0] + '_p1'] = (80, 120)
        self.dynamic = True
        self.assertEqual(self.run_list([{'url': self.ids[0]}]), 0)
        record = library.get_video_info(self.settings, self.ids[0])
        self.assertEqual([(item['quality_id'], item['status']) for item in record.data['files']], [(120, 'failed'), (80, 'completed')])
        self.input.assert_not_called()

    def test_mixed_quality_groups_fail_independently_without_prompt(self) -> None:
        """验证不同分P分别取最高档，混档组失败而另一组成功并写独立合并索引；无返回值。"""
        self.counts[self.ids[0]] = 2
        self.catalogs[self.ids[0] + '_p1'] = (80, 120)
        self.assertEqual(self.run_list([{'url': self.ids[0], 'merge-parts': '1,2;2,2'}]), 2)
        source = library.get_video_info(self.settings, self.ids[0])
        self.assertEqual({item['quality_id'] for item in source.data['files']}, {80, 120})
        index = MergedVideoInfo(self.settings.output_dir, source.quality_directory(Quality(80, '画质80'), role='merged'))
        self.assertEqual([item['source_parts'] for item in index.data['files']], [[2, 2]])
        self.input.assert_not_called()

    def test_failure_and_missing_rank_do_not_stop_other_post(self) -> None:
        """验证下载失败或排名不存在时，其他投稿仍完成并以2汇总退出；无返回值。"""
        self.failed.add(self.ids[0] + '_p1')
        self.assertEqual(self.run_list([{'url': bvid} for bvid in self.ids]), 2)
        self.assertEqual(library.get_video_info(self.settings, self.ids[0]).data['files'][0]['status'], 'failed')
        self.assertEqual(library.get_video_info(self.settings, self.ids[1]).data['files'][0]['status'], 'completed')
        self.failed.clear()
        self.assertEqual(self.run_list([{'url': self.ids[0], 'quality': 2}, {'url': self.ids[1]}]), 2)
        self.input.assert_not_called()

    def test_cancel_marks_running_part_and_keeps_partial(self) -> None:
        """验证Ctrl+C协作停止正在下载的分P，保存 interrupted 和断点，未派发分P保持待处理；无返回值。"""
        self.counts[self.ids[0]], self.cancel = 2, True
        self.settings = replace(self.settings, threads=1)
        self.enterContext(patch.object(config, 'load_settings', return_value=self.settings))
        self.assertEqual(self.run_list([{'url': self.ids[0]}]), 130)
        source = library.get_video_info(self.settings, self.ids[0])
        states = {item['part']: item['status'] for item in source.data['files']}
        self.assertEqual(states[1], 'interrupted')
        self.assertIn(states[2], {'pending', 'interrupted'})
        self.assertEqual(self.calls, {self.ids[0] + '_p1': 1})
        self.assertTrue(list(self.settings.output_dir.rglob('*.part')))
        run = self.read_log()['runs'][-1]
        self.assertEqual(run['status'], 'interrupted')
        self.assertFalse(any(row['status'] in ('downloading', 'pending', 'queued', 'merging') for row in run['entries']))


    def test_first_download_starts_before_later_directory_is_resolved(self) -> None:
        """走正式入口，后续独立投稿查询目录时首项已开始媒体传输，避免只验证底层调度器。"""
        original = self.resolve_targets

        def resolve(context, url, settings, all_parts):
            """保持 context/url/settings/all_parts 协议；第二投稿须等待第一投稿实际下载开始。"""
            if self.ids[1] in url:
                self.assertTrue(self.started.wait(5), '首项下载不应等待后续目录')
            return original(context, url, settings, all_parts)

        with patch.object(api, 'resolve_targets', side_effect=resolve):
            self.assertEqual(self.run_list([{'url':value} for value in self.ids]), 0)
        self.assertEqual(set(self.calls), {value+'_p1' for value in self.ids})


class ConcurrentRecordTests(unittest.TestCase):
    """以实际JSON和并发屏障验证锁范围及独立HTTP会话。"""

    def test_record_read_finishes_before_atomic_replacement(self) -> None:
        """保持真实读句柄并发写入时，写者等待读取结束；读者见旧完整记录，新读者见新记录，不发生访问拒绝。"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'video_info.json'
            path.write_text('{"state": "old"}', encoding='utf-8')
            reading, release, writing = Event(), Event(), Event()
            original = Path.open

            def open_record(value: Path, *args: object, **kwargs: object) -> object:
                """按 args/kwargs 打开 value，对当前记录的读取保留真实句柄直到测试放行，返回文本流。"""
                stream = original(value, *args, **kwargs)
                mode = args[0] if args else kwargs.get('mode', 'r')
                if value == path and mode == 'r':
                    read = stream.read

                    def paused_read(*read_args: object, **read_kwargs: object) -> str:
                        """按 read_args/read_kwargs 读取后等待放行，保持句柄打开；返回原文本。"""
                        text = read(*read_args, **read_kwargs)
                        reading.set()
                        self.assertTrue(release.wait(5))
                        return text

                    stream.read = paused_read
                return stream

            def write_record() -> None:
                """标记写任务已执行，并用真实原子接口更新记录；无参数及返回值。"""
                writing.set()
                metadata.atomic_text(path, '{"state": "new"}')

            with patch.object(Path, 'open', open_record), ThreadPoolExecutor(max_workers=2) as pool:
                reader = pool.submit(metadata.read_json, path)
                try:
                    self.assertTrue(reading.wait(5))
                    writer = pool.submit(write_record)
                    self.assertTrue(writing.wait(5))
                    self.assertFalse(writer.done())
                finally:
                    release.set()
                self.assertEqual(reader.result(timeout=5), {'state': 'old'})
                writer.result(timeout=5)
            self.assertEqual(metadata.read_json(path), {'state': 'new'})

    def test_http_sessions_copy_cookies_and_downloaders(self) -> None:
        """验证两个工作会话的CookieJar和下载器独立，快照Cookie值不被会话改写；无返回值。"""
        settings = config.load_settings(CONFIG_PATH)
        snapshot = fake_auth_snapshot()
        with auth.worker_session(snapshot, settings) as first, auth.worker_session(snapshot, settings) as second:
            self.assertIsNot(first.downloader, second.downloader)
            next(iter(first.downloader.cookiejar)).value = 'changed'
            self.assertEqual(next(iter(second.downloader.cookiejar)).value, 'fake-only')
            self.assertEqual(snapshot.cookies[0]['value'], 'fake-only')

    def test_same_post_downloads_and_probes_overlap_without_losing_json(self) -> None:
        """验证同一BV三个分P可同时传输及探测，JSON最终保留全部完成状态，锁不覆盖这些耗时步骤；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            root, bvid = Path(directory).resolve(), 'BV1xx411c7mD'
            settings = replace(config.load_settings(CONFIG_PATH), output_dir=root)
            record = library.get_video_info(settings, bvid, '并发投稿', 3)
            quality, download_barrier, probe_barrier = Quality(80, '1080P'), Barrier(3), Barrier(3)
            for part in (1, 2, 3):
                record.entry(f'{bvid}_p{part}', part=part, quality=quality)
            record.save()

            def save(part: int) -> tuple[Path, str]:
                """用独立下载器保存 part，并在传输处等待其他分P，返回目录及前缀。"""
                downloader = MagicMock()

                def dl(filename: str, info: dict) -> tuple[bool, bool]:
                    """将 info 的流写入 filename，屏障确保并发进入传输，返回成功标志。"""
                    Path(filename).write_bytes(b'stream')
                    download_barrier.wait(timeout=5)
                    return True, True

                downloader.dl.side_effect = dl
                info = {'id': f'{bvid}_p{part}', 'title': f'分P{part}', 'selected_quality': {'code': 80, 'name': '1080P'},
                        'requested_formats': [{'format_id': 'raw', 'url': 'https://example.com/video', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'none'}]}
                return transfer.save_video(downloader, info, settings, 'ffmpeg', '并发投稿', 3, source_cid=part, prepared=True)

            def mux(executable: str, sources: list[Path], formats: list[dict], folder: Path, stem: str) -> Path:
                """模拟 executable 将 sources/formats 封装到 folder/stem，返回媒体路径。"""
                path = folder / (stem + '.mp4')
                path.write_bytes(b'video')
                return path

            def probe(executable: str, path: Path) -> dict:
                """使用 executable 探测 path，屏障验证探测不持有BV锁，返回属性。"""
                probe_barrier.wait(timeout=5)
                return {'duration_seconds': 1, 'width': 64, 'height': 64, 'fps': 5, 'video_codec': 'h264', 'audio_codec': None, 'errors': []}

            with patch.object(media, 'mux_streams', side_effect=mux), patch.object(metadata, 'probe_media', side_effect=probe), ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(save, (1, 2, 3)))
            stored = library.get_video_info(settings, bvid)
            self.assertEqual({item['part'] for item in stored.data['files']}, {1, 2, 3})
            self.assertTrue(all(item['status'] == 'completed' and item['duration_seconds'] == 1 for item in stored.data['files']))


if __name__ == '__main__':
    unittest.main()
