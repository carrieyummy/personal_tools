"""验证所选媒体大小、回调修正、去重汇总、真实下载协调及网页和离线报告；仅使用隔离数据。"""

from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from bilibili_downloader import application, config, media, workflow
from bilibili_downloader.activity_log import HtmlLog
from tests import CONFIG_PATH
from tests import test_batch_download as batch
from tests import test_bangumi as bangumi_tests
import download_bilibili as cli


class FileSizeTests(unittest.TestCase):
    """以可控数据验证准确、估算、未知、流切换和去重口径。"""

    def test_selected_streams_require_all_sizes_and_mark_estimates(self) -> None:
        """验证音视频合计、估算标记、非法值及未知流；不把未知长度当作零，无参数和返回值。"""
        self.assertEqual(media.expected_size([{'filesize': 8_000_000}, {'filesize': 2_000_000}]),
                         {'expected_size_bytes': 10_000_000, 'expected_size_estimated': False})
        self.assertEqual(media.expected_size([{'filesize': 8_000_000}, {'filesize_approx': 2_000_000}]),
                         {'expected_size_bytes': 10_000_000, 'expected_size_estimated': True})
        for value in (None, 0, -1, True, '1000', float('inf'), float('nan')):
            with self.subTest(value=value):
                self.assertIsNone(media.expected_size([{'filesize': 8_000_000}, {'filesize': value}])['expected_size_bytes'])
        self.assertIsNone(media.expected_size([])['expected_size_bytes'])
        self.assertEqual(media.format_size(2_500_000_000), '2.50 GB')
        self.assertEqual(media.file_size_fields({'expected_size_bytes': 100, 'actual_size_bytes': 2_000_000})['file_size_text'],
                         '实际 2.00 MB')

    def test_stream_switch_resume_and_retry_replace_instead_of_accumulate(self) -> None:
        """验证断点字节不冒充完整长度，重试及切换流仅替换对应大小，估算不覆盖准确值；无返回值。"""
        sizes = media.StreamSizes([{}, {}])
        sizes.start(0)
        sizes.progress({'status': 'downloading', 'downloaded_bytes': 80})
        self.assertIsNone(sizes.expected()['expected_size_bytes'])
        sizes.progress({'total_bytes': 100, 'downloaded_bytes': 90})
        sizes.progress({'total_bytes_estimate': 999, 'downloaded_bytes': 2})
        sizes.start(1)
        sizes.progress({'total_bytes_estimate': 20, 'downloaded_bytes': 4})
        self.assertEqual(sizes.expected(), {'expected_size_bytes': 120, 'expected_size_estimated': True})
        sizes.progress({'status': 'finished', 'downloaded_bytes': 24})
        sizes.start(0)
        sizes.progress({'status': 'finished', 'downloaded_bytes': 100})
        self.assertEqual(sizes.expected(), {'expected_size_bytes': 124, 'expected_size_estimated': False})

    def test_totals_deduplicate_versions_include_reuse_and_exclude_merges(self) -> None:
        """验证重复任务选实际大小、不同版本分别统计、复用包含且合并排除；未知保持显式，无返回值。"""
        entries = [{'kind': '下载', 'download_key': ['same', 1], 'expected_size_bytes': 9_000_000},
                   {'kind': '下载', 'download_key': ['same', 1], 'status': 'reused', 'actual_size_bytes': 6_000_000},
                   {'kind': '下载', 'download_key': ['same', 2], 'expected_size_bytes': 8_000_000,
                    'expected_size_estimated': True}, {'kind': '下载', 'id': 'unknown'},
                   {'kind': '合并', 'actual_size_bytes': 99_000_000}]
        total = media.summarize_sizes(entries)
        self.assertEqual((total['known_bytes'], total['file_count'], total['unknown_count']), (14_000_000, 3, 1))
        self.assertIn('约 14.00 MB', total['text'])
        self.assertIn('另 1 个文件大小未知', total['text'])
        self.assertEqual(media.file_size_fields({})['file_size_text'], '未知')


class FileSizeIntegrationTests(unittest.TestCase):
    """复用隔离下载驱动器验证准备、派发、复用及失败后的大小信息，不读取真实账号。"""

    def setUp(self) -> None:
        """创建串行离线驱动器和可控大小元数据，避免测试依赖用户并发配置；无参数和返回值。"""
        self.engine = batch.BatchEngineTests()
        self.engine.setUp()
        self.addCleanup(self.engine.doCleanups)
        self.engine.settings = replace(self.engine.settings, threads=1)
        self.enterContext(patch.object(config, 'load_settings', return_value=self.engine.settings))
        self.sizes = {}
        original = batch.FakeDownloader.extract_info

        def extract(downloader: batch.FakeDownloader, url: str, **options: object) -> dict:
            """对 downloader/url/options 取得原离线格式，并注入本例配置的大小；返回原始格式信息。"""
            info = original(downloader, url, **options)
            for fmt in info['formats']:
                fmt.update(self.sizes.get((info['id'], fmt['quality']), {}))
            return info

        self.enterContext(patch.object(batch.FakeDownloader, 'extract_info', extract))

    def test_plan_is_visible_before_transfer_and_duplicate_results_share_actual_size(self) -> None:
        """逐项传输前须登记该项预计大小，后续重复任务可继续发现并共享最终实际大小；无返回值。"""
        engine, source = self.engine, self.engine.ids[0]
        self.sizes[source + '_p1', 80] = {'filesize': 10_000_000}
        original = engine.transfer

        def transfer(filename, info, hooks):
            """实际传输 filename/info 前核对已发现计划，hooks 透传；不要求后续任务提前全部登记。"""
            entries = engine.read_log()['runs'][-1]['entries']
            downloads = [entry for entry in entries if entry['kind'] == '下载']
            self.assertTrue(downloads)
            self.assertTrue(all(entry['expected_size_bytes'] == 10_000_000 for entry in downloads))
            self.assertEqual(media.summarize_sizes(entries)['file_count'], 1)
            self.assertFalse(engine.calls)
            return original(filename, info, hooks)

        tasks = [{'url': source}, {'url': source}]
        with patch.object(engine, 'transfer', side_effect=transfer):
            self.assertEqual(engine.run_list(tasks), 0, engine.stderr.getvalue())
        entries = engine.read_log()['runs'][-1]['entries']
        downloads = [entry for entry in entries if entry['kind'] == '下载']
        actual = Path(downloads[0]['path']).stat().st_size
        self.assertEqual([entry['actual_size_bytes'] for entry in downloads], [actual, actual])
        self.assertEqual(media.summarize_sizes(entries)['known_bytes'], actual)
        self.assertEqual(sum(engine.calls.values()), 1)
        self.assertIn('预计 10.00 MB', engine.stdout.getvalue())
        self.assertIn('本次运行最终目标视频总大小', engine.stdout.getvalue())
        self.assertEqual(engine.run_list(tasks), 0, engine.stderr.getvalue())
        self.assertEqual(sum(engine.calls.values()), 1)
        self.assertTrue(all(entry['actual_size_bytes'] == actual for entry in engine.read_log()['runs'][-1]['entries']
                            if entry['kind'] == '下载'))

    def test_different_quality_versions_are_counted_separately(self) -> None:
        """验证同一 P 的两个实际画质是两份目标文件，预计值按各自所选流计算；无返回值。"""
        engine, source = self.engine, self.engine.ids[0]
        engine.catalogs[source + '_p1'] = (80, 120)
        self.sizes[source + '_p1', 80] = {'filesize': 2_000_000}
        self.sizes[source + '_p1', 120] = {'filesize_approx': 4_000_000}
        self.assertEqual(engine.run_list([{'url': source, 'quality': 1}, {'url': source, 'quality': 2}]), 0,
                         engine.stderr.getvalue())
        downloads = [entry for entry in engine.read_log()['runs'][-1]['entries'] if entry['kind'] == '下载']
        self.assertEqual(media.summarize_sizes(downloads)['file_count'], 2)
        self.assertEqual(sum(engine.calls.values()), 2)
        self.assertIn('预计 约 4.00 MB', engine.stdout.getvalue())
        self.assertIn('预计 2.00 MB', engine.stdout.getvalue())

    def test_failed_unknown_target_is_not_hidden_from_total(self) -> None:
        """验证失败且未知大小的目标仍显示未知数量，成功文件使用实际字节数；无返回值。"""
        engine = self.engine
        engine.failed.add(engine.ids[1] + '_p1')
        self.assertEqual(engine.run_list([{'url': value} for value in engine.ids]), 2)
        total = media.summarize_sizes(engine.read_log()['runs'][-1]['entries'])
        self.assertEqual((total['file_count'], total['unknown_count']), (2, 1))
        self.assertGreater(total['known_bytes'], 0)
        self.assertIn('另 1 个文件大小未知', total['text'])

    def test_progress_supplies_missing_size_without_extra_extraction(self) -> None:
        """验证仅下载回调提供大小时网页即时补齐、终端补报一次，且没有额外原站提取；无返回值。"""
        engine, source = self.engine, self.engine.ids[0]
        original = engine.transfer

        def transfer(filename: str, info: dict, hooks: list) -> tuple[bool, bool]:
            """对 filename/info/hooks 模拟现有回调提供完整长度，检查日志后继续原离线传输；返回成功状态。"""
            for hook in hooks:
                hook({'status': 'downloading', 'downloaded_bytes': 1, 'total_bytes': 8_000_000})
            entry = next(entry for entry in engine.read_log()['runs'][-1]['entries'] if entry['kind'] == '下载')
            self.assertEqual(entry['expected_size_bytes'], 8_000_000)
            self.assertIsNone(entry.get('actual_size_bytes'))
            return original(filename, info, hooks)

        engine.transfer = transfer
        self.assertEqual(engine.run_list([{'url': source}]), 0, engine.stderr.getvalue())
        self.assertEqual(engine.extracts[source + '_p1'], 2)
        self.assertEqual(engine.stdout.getvalue().count('文件大小：预计 8.00 MB'), 1)

    def test_dispatch_format_change_clears_previously_known_actual_size(self) -> None:
        """验证派发前格式编号变化时清除旧成品大小，即使编码版本摘要未变也不误报复用大小；无返回值。"""
        engine, source = self.engine, self.engine.ids[0]
        self.sizes[source + '_p1', 80] = {'filesize': 10_000_000}
        self.assertEqual(engine.run_list([{'url': source}]), 0, engine.stderr.getvalue())
        original_extract, original_transfer = batch.FakeDownloader.extract_info, engine.transfer
        refresh_at = engine.extracts[source + '_p1'] + 2

        def extract(downloader: batch.FakeDownloader, url: str, **options: object) -> dict:
            """仅在刷新派发时改变 downloader/url/options 的格式编号；返回仍属同编码版本的流信息。"""
            info = original_extract(downloader, url, **options)
            if engine.extracts[info['id']] >= refresh_at:
                info['formats'][0]['format_id'] = 'new-stream-id'
            return info

        def transfer(filename: str, info: dict, hooks: list) -> tuple[bool, bool]:
            """在 filename/info/hooks 真实离线传输前检查旧大小已清除；返回原传输结果。"""
            entry = next(entry for entry in engine.read_log()['runs'][-1]['entries'] if entry['kind'] == '下载')
            self.assertIsNone(entry.get('actual_size_bytes'))
            self.assertEqual(entry['expected_size_bytes'], 10_000_000)
            return original_transfer(filename, info, hooks)

        engine.transfer = transfer
        with patch.object(batch.FakeDownloader, 'extract_info', extract):
            self.assertEqual(engine.run_list([{'url': source}]), 0, engine.stderr.getvalue())
        self.assertEqual(sum(engine.calls.values()), 2)


class FileSizePageTests(unittest.TestCase):
    """检查真实日志服务及浏览器列表、汇总、筛选、详情和离线快照。"""

    def setUp(self) -> None:
        """创建临时日志目录和本机随机端口配置；无参数和返回值。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = replace(config.load_settings(CONFIG_PATH), output_dir=self.root, log_port=0,
                                log_refresh_interval=0.1)
        self.enterContext(redirect_stdout(StringIO()))

    def test_snapshot_shares_duplicate_sizes_and_keeps_history_unknown(self) -> None:
        """验证同一版本共享最新大小、任务与运行分别去重、旧条目未知且 API 不读磁盘；无返回值。"""
        with HtmlLog(self.settings, '大小测试') as journal:
            first = journal.add('下载', '第一季', task='season1', part='P1')
            first.update(download_key=['ss1', 1], expected_size_bytes=10_000_000)
            second = journal.add('下载', '同一季重复任务', task='duplicate', part='P1')
            second.update(download_key=['ss1', 1])
            journal.add('下载', '旧记录', task='legacy', part='P1')
            merged = journal.add('合并', '第一季', task='season1')
            merged.update(actual_size_bytes=999_000_000)
            with patch.object(Path, 'read_text', side_effect=AssertionError('网页不可重新读盘')):
                with urlopen(journal.url + 'api/state', timeout=5) as response:
                    state = json.load(response)['runs'][-1]
            self.assertEqual(state['size_summary']['file_count'], 2)
            self.assertEqual(state['size_summary']['unknown_count'], 1)
            self.assertEqual(state['task_sizes']['duplicate']['known_bytes'], 10_000_000)
            self.assertEqual(state['entries'][1]['file_size_text'], '预计 10.00 MB')
            self.assertEqual(state['entries'][2]['file_size_text'], '未知')
            self.assertEqual(second.entry.get('expected_size_bytes'), None)

    def test_browser_sizes_live_update_and_offline_report_ignore_filters(self) -> None:
        """验证文件大小列和详情、筛选不改变总大小、实际值实时更新，以及离线快照固定大小；无返回值。"""
        from playwright.sync_api import sync_playwright

        with HtmlLog(self.settings, '网页大小测试') as journal, sync_playwright() as playwright:
            entry = journal.add('下载', '第一季', task='season1', part='P1')
            entry.update(download_key=['ss1', 1], expected_size_bytes=12_000_000, expected_size_estimated=True)
            journal.add('下载', '未知大小', task='season2', part='P2')
            browser = playwright.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            try:
                page = browser.new_page(viewport={'width': 1600, 'height': 900})
                page.goto(journal.url)
                self.assertIn('文件大小', page.locator('thead').inner_text())
                self.assertIn('预计 约 12.00 MB', page.locator('.entry-row .size-cell').first.inner_text())
                before = page.locator('.run-size-total').inner_text()
                page.locator('#search').fill('未知大小')
                self.assertEqual(page.locator('.run-size-total').inner_text(), before)
                page.locator('#search').fill('第一季')
                entry.update('completed', actual_size_bytes=11_000_000)
                page.wait_for_function("document.querySelector('.entry-row .size-cell').textContent === '实际 11.00 MB'")
                page.locator('.entry-row button').click()
                self.assertIn('11.00 MB', page.locator('.detail-panel').inner_text())
                page.locator('#search').fill('')
                page.screenshot(path=str(Path(tempfile.gettempdir()) / 'bilibili-file-sizes-desktop.png'), full_page=True)
                exported = self.root / 'offline_size.html'
                exported.write_text(journal.render_document(live=False), encoding='utf-8')
                offline = browser.new_page()
                offline.goto(exported.as_uri())
                self.assertIn('已知合计 11.00 MB', offline.locator('.run-size-total').inner_text())
                entry.update(actual_size_bytes=15_000_000)
                page.wait_for_function("document.querySelector('.run-size-total').textContent.includes('15.00 MB')")
                self.assertIn('11.00 MB', offline.locator('.run-size-total').inner_text())
                page.set_viewport_size({'width': 1280, 'height': 900})
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 1280)
            finally:
                browser.close()


class SeasonFileSizeTests(unittest.TestCase):
    """使用番剧离线驱动器验证所有季独立大小及本次运行合计。"""

    def test_all_seasons_have_separate_task_totals(self) -> None:
        """下载两季并逐季校验实际大小及运行去重合计，排除合并成品；无参数和返回值。"""
        engine = bangumi_tests.BangumiIntegrationTests()
        engine.setUp()
        self.addCleanup(engine.doCleanups)
        engine.add_related_season()
        engine.settings = replace(engine.settings, all_seasons=True, log_port=0)
        tasks = engine.root / 'sizes.yaml'
        tasks.write_text('quality: 1\nall-parts: false\nmerge-parts: 0\ndownload_list:\n  - url: ss28541\n', encoding='utf-8')
        with HtmlLog(engine.settings, '所有季大小') as journal:
            self.assertEqual(application.run_with_settings(cli.parse_args(['--url', str(tasks)]), engine.settings, journal), 0,
                             engine.stderr.getvalue())
            run = journal.snapshot(live=False)['runs'][-1]
            downloads = [entry for entry in run['entries'] if entry['kind'] == '下载']
            self.assertEqual(len(downloads), 6)
            self.assertEqual(run['size_summary']['file_count'], 6)
            self.assertEqual(run['size_summary']['known_bytes'], sum(Path(entry['path']).stat().st_size for entry in downloads))
            self.assertEqual(len(run['task_sizes']), 2)
            self.assertTrue(all(total['file_count'] == 3 and total['unknown_count'] == 0 for total in run['task_sizes'].values()))
