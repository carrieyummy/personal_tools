"""验证实时日志服务、JSON 持久化、历史导入及不可变离线快照。"""

from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import re
import socket
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bilibili_downloader.activity_log import HtmlLog
from bilibili_downloader.config import load_settings
from bilibili_downloader.errors import ToolError
from tests import PROJECT_DIR


class ActivityLogTests(unittest.TestCase):
    """使用隔离目录及本机服务检查真实落盘与浏览器行为，不读取用户下载记录。"""

    def setUp(self) -> None:
        """创建隔离输出及快速轮询配置；无参数和返回值。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = replace(load_settings(PROJECT_DIR / 'config.yaml'), output_dir=self.root,
                                log_refresh_interval=0.1, log_write_retry_interval=0.001)
        self.enterContext(redirect_stderr(StringIO()))

    def read(self) -> dict:
        """读取隔离目录的持久化 JSON；返回结构化日志，无参数。"""
        return json.loads((self.root / self.settings.log_state_filename).read_text(encoding='utf-8'))

    def test_progress_is_live_throttled_and_state_changes_are_immediate(self) -> None:
        """验证进度节流、状态落盘及 API 从内存读取，无返回值。"""
        with HtmlLog(self.settings, '下载视频') as journal:
            activity = journal.add('下载', '测试', part='P1')
            with patch('bilibili_downloader.activity_log.time.monotonic', side_effect=[100, 100.1]):
                activity.progress({'downloaded_bytes': 25, 'total_bytes': 100})
                activity.progress({'downloaded_bytes': 70, 'total_bytes': 100})
            self.assertEqual(self.read()['runs'][-1]['entries'][0]['progress'], 25)
            with patch.object(Path, 'read_text', side_effect=AssertionError('API 不得读取日志磁盘')):
                with urlopen(journal.url + 'api/state', timeout=5) as response:
                    state = json.load(response)
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(state['runs'][-1]['entries'][0]['progress'], 25)
            activity.update('completed', detail='下载完成')
            self.assertEqual(self.read()['runs'][-1]['entries'][0]['status'], 'completed')
        self.assertEqual(self.read()['runs'][-1]['status'], 'completed')
        self.assertTrue(journal.snapshot_path.is_file())

    def test_legacy_import_repairs_closed_run_without_changing_old_html(self) -> None:
        """验证导入旧 HTML，修正结束后的活动状态，同时保留旧文件及原始失败；无返回值。"""
        old = {'version': 1, 'runs': [{'label': '下载视频', 'started': '2026-11-15T00:51:21+08:00',
               'finished': '2026-11-15T00:58:20+08:00', 'status': 'interrupted', 'detail': '', 'entries': [
                   {'status': 'downloading', 'detail': '残留进度', 'finished': ''},
                   {'status': 'failed', 'detail': '[WinError 5] 原始错误'}]}]}
        # 旧日志条目也应具有展示字段，避免测试依赖残缺数据。
        for entry in old['runs'][0]['entries']:
            entry.update(kind='下载', title='测试', task='BV', part='P1', started='2026-11-15T00:51:21+08:00',
                         updated='2026-11-15T00:59:00+08:00', path='', quality='', progress=None)
        path = self.root / self.settings.log_filename
        path.write_text('<script id="download-log-data" type="application/json">' + json.dumps(old) + '</script>', encoding='utf-8')
        previous = path.read_bytes()
        with HtmlLog(self.settings, '下载视频') as journal:
            entries = journal.data['runs'][0]['entries']
            self.assertEqual([entry['status'] for entry in entries], ['interrupted', 'failed'])
            self.assertEqual(entries[1]['detail'], '[WinError 5] 原始错误')
        self.assertEqual(path.read_bytes(), previous)
        with HtmlLog(self.settings, '离线拼接') as journal:
            journal.add('合并', '第二次', status='completed')
        self.assertEqual(len(self.read()['runs']), 3)

    def test_write_failure_preserves_previous_json_and_recovers_latest_state(self) -> None:
        """验证持久化失败不向下载抛错、只警告一次，恢复后保存最新状态并清理临时文件；无返回值。"""
        with HtmlLog(self.settings, '下载视频') as journal:
            previous = journal.path.read_bytes()
            warning = StringIO()
            with patch.object(Path, 'replace', side_effect=OSError('磁盘写入失败')), redirect_stderr(warning):
                activity = journal.add('下载', '测试', status='downloading')
                activity.update('completed', path=str(self.root / '视频.mp4'))
            self.assertEqual(warning.getvalue().count('日志暂时无法保存'), 1)
            self.assertEqual(journal.path.read_bytes(), previous)
            self.assertIn('磁盘写入失败', journal.snapshot(live=True)['storage_error'])
            self.assertEqual(list(self.root.glob('*.tmp')), [])
            journal.add('任务', '保存恢复', status='completed')
            self.assertEqual(self.read()['runs'][-1]['entries'][0]['status'], 'completed')
            self.assertEqual(self.read()['storage_error'], '')

    def test_windows_sharing_error_has_bounded_retries(self) -> None:
        """验证 Windows 文件占用错误有限重试，成功后继续且不会将活动标为失败；无返回值。"""
        with HtmlLog(self.settings, '下载视频') as journal:
            original = Path.replace
            error = PermissionError('文件占用')
            error.winerror = 5
            attempts = []

            def replace_file(source: Path, destination: Path) -> Path:
                """前两次对 source/destination 模拟占用，之后执行真实替换并返回目标 Path。"""
                attempts.append(source)
                if len(attempts) <= 2:
                    raise error
                return original(source, destination)

            with patch.object(Path, 'replace', replace_file):
                activity = journal.add('下载', '测试', status='completed')
            self.assertEqual(len(attempts), 3)
            self.assertEqual(activity.entry['status'], 'completed')
            with patch.object(Path, 'replace', side_effect=error) as replace_mock:
                activity.update(detail='持续占用')
            self.assertEqual(replace_mock.call_count, self.settings.log_write_retries + 1)

    def test_close_blocks_late_progress_and_stops_server(self) -> None:
        """验证中断关闭后迟到回调不会改写终态或文件，HTTP 服务不再监听；无返回值。"""
        journal = HtmlLog(self.settings, '下载视频')
        activity = journal.add('下载', '测试', status='downloading')
        journal.__exit__(KeyboardInterrupt, KeyboardInterrupt('用户中止'), None)
        previous = journal.path.read_bytes()
        snapshot = journal.snapshot_path.read_bytes()
        activity.update('downloading', detail='迟到更新')
        activity.progress({'downloaded_bytes': 99, 'total_bytes': 100})
        self.assertEqual(journal.path.read_bytes(), previous)
        self.assertEqual(journal.snapshot_path.read_bytes(), snapshot)
        self.assertEqual(activity.entry['status'], 'interrupted')
        with self.assertRaises((URLError, ConnectionError)):
            urlopen(journal.url, timeout=1)

    def test_close_does_not_wait_for_idle_browser_connection_timeout(self) -> None:
        """验证浏览器预连接未发送完整 HTTP 头时，服务关闭也能迅速回收请求线程；无返回值。"""
        journal = self.enterContext(HtmlLog(self.settings, '下载视频'))
        with socket.create_connection(('127.0.0.1', journal.server.http.server_port), timeout=5) as connection:
            connection.sendall(b'GET / HTTP/1.1\r\n')
            deadline = time.monotonic() + 5
            while not journal.server.connections and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(journal.server.connections)
            started = time.monotonic()
            journal.__exit__(KeyboardInterrupt, KeyboardInterrupt(), None)
            self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(journal.server.thread.is_alive())

    def test_export_escaping_and_media_path_limits(self) -> None:
        """验证脚本转义、导出命名唯一，以及 API 只提供已完成且位于输出目录的文件；无返回值。"""
        with HtmlLog(self.settings, '下载视频') as journal:
            title = '</script><script>alert("x")</script>'
            media = self.root / '中文#视频.mp4'
            media.write_bytes(b'media')
            activity = journal.add('下载', title, status='completed')
            activity.update(path=str(media))
            self.assertNotIn(title, journal.render_document(live=False))
            url = journal.snapshot(live=True)['runs'][-1]['entries'][0]['file_url']
            with urlopen(journal.url.rstrip('/') + url, timeout=5) as response:
                self.assertEqual(response.read(), b'media')
            with self.assertRaises(HTTPError) as missing:
                urlopen(journal.url + 'files/../config.yaml', timeout=5)
            self.assertEqual(missing.exception.code, 404)
            with self.assertRaises(HTTPError) as denied:
                urlopen(Request(journal.url, headers={'Host': 'evil.example'}), timeout=5)
            self.assertEqual(denied.exception.code, 403)
            names = []
            for _ in range(2):
                with urlopen(journal.url + 'snapshot', timeout=5) as response:
                    names.append(response.headers['Content-Disposition'])
                    self.assertIn('const live = false', response.read().decode())
            self.assertNotEqual(*names)

    def test_invalid_history_is_not_overwritten(self) -> None:
        """验证损坏的历史不会被新运行覆盖；无返回值。"""
        path = self.root / self.settings.log_state_filename
        path.write_text('invalid', encoding='utf-8')
        with self.assertRaises(ToolError):
            HtmlLog(self.settings, '下载视频')
        self.assertEqual(path.read_text(encoding='utf-8'), 'invalid')

    def test_transfer_average_excludes_resume_and_gaps_between_streams(self) -> None:
        """验证已有断点不计入本次字节，音视频速度按有效传输时长累计，最终样本不受节流限制；无返回值。"""
        with HtmlLog(self.settings, '下载视频') as journal:
            activity = journal.add('下载', '视频', part='P1')
            with patch('bilibili_downloader.activity_log.time.monotonic', side_effect=[10, 12, 14, 15, 25, 27, 28]):
                activity.start_transfer('视频流', 100_000_000)
                activity.progress({'status': 'downloading', 'downloaded_bytes': 150_000_000, 'total_bytes': 200_000_000})
                self.assertEqual(activity.entry['average_speed_bps'], 25_000_000)
                activity.progress({'status': 'finished', 'downloaded_bytes': 200_000_000, 'total_bytes': 200_000_000})
                activity.end_transfer()
                activity.start_transfer('音频流')
                activity.progress({'status': 'finished', 'downloaded_bytes': 20_000_000, 'total_bytes': 20_000_000})
                activity.end_transfer()
            self.assertEqual(activity.entry['processed_bytes'], 120_000_000)
            self.assertEqual(activity.entry['metric_seconds'], 8)
            self.assertEqual(activity.entry['average_speed_bps'], 15_000_000)
            self.assertEqual(len(activity.entry['metric_intervals']), 2)
            activity.update('processing', progress=None)
            self.assertEqual(activity.entry['average_speed_bps'], 15_000_000)

    def test_merge_reports_media_progress_output_average_and_unknown_duration(self) -> None:
        """验证 FFmpeg 媒体时间、输出吞吐量及剩余时间，未知时长不伪造百分比；无返回值。"""
        with HtmlLog(self.settings, '离线拼接') as journal:
            activity = journal.add('合并', '视频', status='merging')
            with patch('bilibili_downloader.activity_log.time.monotonic', side_effect=[100, 102, 110]):
                activity.start_merge(10, 100_000_000)
                activity.merge_progress({'out_time_us': '2500000', 'total_size': '20000000', 'progress': 'continue'})
                self.assertEqual(activity.entry['progress'], 25)
                self.assertEqual(activity.entry['average_speed_bps'], 10_000_000)
                self.assertEqual(activity.entry['eta_seconds'], 6)
                activity.merge_progress({'out_time_us': '10000000', 'total_size': '100000000', 'progress': 'end'})
            self.assertLess(activity.entry['progress'], 100)
            activity.update('completed')
            self.assertEqual(activity.entry['progress'], 100)
            unknown = journal.add('合并', '缺失元数据', status='merging')
            unknown.start_merge(None, 100)
            unknown.merge_progress({'out_time_us': 'N/A', 'total_size': 'N/A', 'progress': 'end'})
            self.assertIsNone(unknown.entry['progress'])
            self.assertIsNone(unknown.entry['average_speed_bps'])

    def test_planned_merge_is_claimed_once_and_entry_identity_is_stable(self) -> None:
        """验证计划与执行共用同一合并项，网页刷新及历史加载均保留稳定 ID；无返回值。"""
        with HtmlLog(self.settings, '下载视频') as journal:
            planned = journal.add('合并', '视频', task='任务 1', part='全部分 P')
            activity = journal.merge_activity('视频', task='任务 1', part='全部分 P')
            self.assertEqual(activity.entry['id'], planned.entry['id'])
            self.assertEqual(len(journal.run['entries']), 1)
            activity.update('completed')
            identity = activity.entry['id']
        with HtmlLog(self.settings, '第二次') as journal:
            self.assertEqual(journal.data['runs'][0]['entries'][0]['id'], identity)

    def test_logging_config_validation(self) -> None:
        """验证日志文件、服务端口、重试次数及间隔约束；无返回值。"""
        original = (PROJECT_DIR / 'config.yaml').read_text(encoding='utf-8')
        config = self.root / 'config.yaml'
        cases = [('html_file: "download_log.html"', 'html_file: "../bad.html"'),
                 ('state_file: "download_log.json"', 'state_file: "bad.json/extra"'),
                 ('port: 0', 'port: 65536'), ('write_retries: 5', 'write_retries: -1'),
                 ('write_retry_interval_seconds: 0.2', 'write_retry_interval_seconds: 0')]
        for old, new in cases:
            with self.subTest(new=new):
                config.write_text(original.replace(old, new), encoding='utf-8')
                with self.assertRaises(ToolError):
                    load_settings(config)

    def test_chrome_tabs_poll_without_opening_persistent_files_and_export_offline(self) -> None:
        """验证桌面树形层级、筛选、指标、跨刷新选择与折叠，以及多标签页及不可变离线快照；无返回值。"""
        from playwright.sync_api import sync_playwright

        journal = self.enterContext(HtmlLog(self.settings, '下载视频'))
        success = journal.add('下载', '高等数学：多元函数', task='任务 1 · BV1xx411c7mD', part='P1', status='completed')
        success.update(quality='1080P 高清')
        live = journal.add('下载', '机器学习：线性回归', task='任务 2 · BV1yy411c7mD', part='P2', status='downloading')
        live.update(progress=64, progress_label='视频流', current_bytes=512_000_000, total_bytes=800_000_000,
                    processed_bytes=512_000_000, metric_seconds=52, average_speed_bps=512_000_000 / 52, eta_seconds=29)
        journal.add('合并', '高等数学：多元函数', task='任务 1 · BV1xx411c7mD', part='第 1 组 [1-3]', status='merging')
        journal.add('下载', '机器学习：线性回归', task='任务 2 · BV1yy411c7mD', part='P3', status='failed', detail='网络中断，断点已保留')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            try:
                page = browser.new_page(viewport={'width': 1600, 'height': 900}, accept_downloads=True)
                other = browser.new_page()
                page.goto(journal.url)
                other.goto(journal.url)
                other.locator('#search').fill('机器学习')
                self.assertEqual(page.locator('.task-row').count(), 2)
                self.assertEqual(page.evaluate('duration(90061)'), '25:01:01')
                self.assertEqual(page.evaluate("downloadCount([{task:'A',source_part:2,status:'completed'},{task:'A',source_part:2,status:'failed'}])"), '0/1 P · 1/2 版本')
                self.assertEqual(page.evaluate("dateTime('2026-11-15T14:30:05+08:00')"), '2026:11:15 14:30:05')
                self.assertAlmostEqual(page.evaluate("groupSpeed({children:[{kind:'下载',processed_bytes:100000000,metric_intervals:[[0,10]]},{kind:'下载',processed_bytes:100000000,metric_intervals:[[5,15]]}]})"), 200_000_000 / 15)
                page.locator('#search').fill('机器学习')
                row = page.locator('.entry-row').filter(has_text='P2')
                self.assertIn('9.85 MB/s', row.inner_text())
                self.assertIn('00:00:29', row.inner_text())
                self.assertEqual(row.locator('[role=progressbar]').get_attribute('aria-valuenow'), '64')
                row.locator('button').click()
                self.assertEqual(page.locator('.detail-panel').get_attribute('data-entry-id'), live.entry['id'])
                page.locator('#errors').check()
                self.assertEqual(page.locator('.entry-row:visible').count(), 1)
                self.assertEqual(page.locator('.task-row:visible').count(), 1)
                live.update('failed', detail='模拟状态实时更新')
                page.wait_for_function("document.getElementById('runs').textContent.includes('模拟状态实时更新')")
                other.wait_for_function("document.getElementById('runs').textContent.includes('模拟状态实时更新')")
                self.assertEqual(page.locator('.entry-row:visible').count(), 2)
                self.assertEqual(page.locator('.detail-panel').get_attribute('data-entry-id'), live.entry['id'])
                self.assertEqual(page.locator('#search').input_value(), '机器学习')
                page.locator('#errors').uncheck()
                page.locator('#search').fill('')
                # 关闭再展开任务，后续轮询仍应保留用户选择及层级状态。
                task = page.locator('.task-row').filter(has_text='机器学习')
                task.locator('button').click()
                self.assertEqual(task.locator('button').get_attribute('aria-expanded'), 'true')
                task.locator('button').click()
                previous_revision = page.evaluate('revision')
                live.update(detail='折叠状态实时更新')
                page.wait_for_function('(previous) => revision > previous', arg=previous_revision)
                self.assertEqual(task.locator('button').get_attribute('aria-expanded'), 'false')
                task.locator('button').click()
                page.screenshot(path=str(Path(tempfile.gettempdir()) / 'personal_tools-log-desktop.png'), full_page=True)
                with page.expect_download() as event:
                    page.locator('#export').click()
                exported = self.root / event.value.suggested_filename
                event.value.save_as(exported)
                previous = exported.read_bytes()
                offline = browser.new_page()
                offline.goto(exported.as_uri())
                self.assertIn('离线快照', offline.locator('#connection').inner_text())
                self.assertFalse(offline.locator('#export').is_visible())
                frozen = offline.locator('.overview-time').inner_text()
                offline.wait_for_timeout(1100)
                self.assertEqual(offline.locator('.overview-time').inner_text(), frozen)
                journal.add('任务', '后续状态', status='completed')
                self.assertEqual(exported.read_bytes(), previous)
                page.set_viewport_size({'width': 1280, 'height': 900})
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 1280)
                journal.__exit__(None, None, None)
                page.wait_for_function("document.getElementById('connection').textContent.includes('连接已断开') || document.getElementById('connection').textContent.includes('运行已结束')")
            finally:
                browser.close()
