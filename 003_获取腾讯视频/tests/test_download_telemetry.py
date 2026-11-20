"""验证 URL 分片下载的实际字节、续签和断点复用统计。"""

from copy import deepcopy
from io import BytesIO
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock

import yaml

from local.playback.errors import AddressExpired, PlaybackError
from local.playback.hls import Playlist, Resource, download_steps


class Response(BytesIO):
    """保存模拟响应 data，提供传输所需的响应头和状态码。"""

    def __init__(self, data: bytes):
        """用 data 初始化可读取响应；无返回值。"""
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}
        self.code = 200

    def read1(self, size: int) -> bytes:
        """读取最多 size 字节，返回响应内容。"""
        return self.read(min(size, 2))


class DownloadTelemetryTests(unittest.TestCase):
    """在临时目录验证真实读取与断点统计。"""

    def test_virtual_ts_checks_first_segment_on_download_and_resume(self):
        """长度正确的非 TS 首片应中止后续下载，重跑复核断点时应得到相同错误；无返回值。"""
        config = yaml.safe_load(Path('configs/config.yaml').read_text(encoding='utf-8'))
        names = ('00_sample.f1.1.ts', '01_sample.f1.1.ts')
        playlist = Playlist([Resource(f'https://example.invalid/{name}', None, 1, stable_path=name)
                             for name in names], 0, 1)
        invalid = bytes(188 * 5)
        transport = Mock()
        transport.open.return_value = Response(invalid)
        with tempfile.TemporaryDirectory(prefix='tencent_telemetry_') as root:
            directory = Path(root)
            for expected_requests in (1, 1):
                steps = download_steps(transport, [playlist], directory, config, Mock(), Event())
                with self.assertRaisesRegex(PlaybackError, '首分片无法识别为 MPEG-TS'):
                    next(steps)
                self.assertEqual(transport.open.call_count, expected_requests)
                self.assertEqual((directory / 'track_0' / '000000.bin').read_bytes(), invalid)
                self.assertFalse((directory / 'track_0' / 'local.m3u8').exists())

    def test_virtual_ts_accepts_transport_stream(self):
        """腾讯虚拟 TS 首片含连续同步字节时，下载后仍应生成封装清单；无返回值。"""
        config = yaml.safe_load(Path('configs/config.yaml').read_text(encoding='utf-8'))
        name = '00_sample.f1.1.ts'
        playlist = Playlist([Resource(f'https://example.invalid/{name}', None, 1, stable_path=name)], 0, 1)
        transport = Mock()
        transport.open.return_value = Response((b'\x47' + bytes(187)) * 5)
        with tempfile.TemporaryDirectory(prefix='tencent_telemetry_') as root:
            steps = download_steps(transport, [playlist], Path(root), config, Mock(), Event())
            with self.assertRaises(StopIteration) as result:
                next(steps)
            manifests, _ = result.exception.value
            self.assertIn('000000.bin', manifests[0].read_text(encoding='utf-8'))

    def test_known_total_and_retry(self):
        """精确字节范围给出总量；失败分片重试时撤销未完成字节；无返回值。"""
        config = deepcopy(yaml.safe_load(Path('configs/config.yaml').read_text(encoding='utf-8')))
        config['logging']['progress_interval_seconds'] = 0
        config['download']['retries'] = max(1, config['download']['retries'])
        playlist = Playlist([Resource('https://example.invalid/a?brs=0&bre=3', None, 1)], 0, 1)

        class Transport:
            """首次响应截断，第二次返回完整分片。"""

            def __init__(self):
                """初始化请求计数；无返回值。"""
                self.calls = 0

            def open(self, url, headers):
                """按请求 url 和 headers 返回响应，首次响应故意截断。"""
                self.calls += 1
                response = Response(b'ab' if self.calls == 1 else b'abcd')
                response.headers['Content-Length'] = '4'
                return response

        with tempfile.TemporaryDirectory(prefix='tencent_telemetry_') as root:
            events, transport = [], Transport()
            steps = download_steps(transport, [playlist], Path(root), config, lambda done, total: None,
                                   Event(), telemetry=lambda **values: events.append(values))
            with self.assertRaises(StopIteration):
                next(steps)
            self.assertEqual([event['downloaded_bytes'] for event in events], [0, 2, 0, 2, 4, 4])
            self.assertTrue(all(event['total_bytes'] == 4 for event in events))
            self.assertEqual(transport.calls, 2)

    def test_unknown_total_refresh_and_resume(self):
        """续签及断点复用不伪造总量或计入传输速度；无返回值。"""
        config = deepcopy(yaml.safe_load(Path('configs/config.yaml').read_text(encoding='utf-8')))
        config['logging']['progress_interval_seconds'] = 0
        config['download']['refresh_attempts'] = max(1, config['download']['refresh_attempts'])
        playlist = Playlist([Resource('https://example.invalid/a', None, 1),
                             Resource('https://example.invalid/b', None, 1)], 0, 1)

        class Transport:
            """首次请求模拟地址失效，之后返回测试分片。"""

            def __init__(self):
                """初始化请求计数；无返回值。"""
                self.calls = 0

            def open(self, url, headers):
                """按 url 返回响应，headers 为请求头；首次请求抛出续签错误。"""
                self.calls += 1
                if self.calls == 1:
                    raise AddressExpired('expired')
                return Response(b'abcd' if url.endswith('/a') else b'xyz')

        with tempfile.TemporaryDirectory(prefix='tencent_telemetry_') as root:
            transport, events = Transport(), []
            progress = lambda done, total: None
            cancel = Event()
            # 通过生成器入口采集读块时的中间值，同时模拟地址续签。
            steps = download_steps(transport, [playlist], Path(root), config, progress, cancel, telemetry=lambda **values: events.append(values))
            self.assertEqual(next(steps), 'refresh')
            with self.assertRaises(StopIteration):
                steps.send([playlist])
            self.assertTrue(any(0 < event['downloaded_bytes'] < 7 and event['total_bytes'] is None for event in events))
            self.assertEqual(events[-1]['downloaded_bytes'], 7)
            self.assertEqual(events[-1]['total_bytes'], 7)
            self.assertGreater(events[-1]['bytes_per_second'], 0)
            resumed = []
            steps = download_steps(transport, [playlist], Path(root), config, progress, cancel, telemetry=lambda **values: resumed.append(values))
            with self.assertRaises(StopIteration):
                next(steps)
            self.assertEqual(transport.calls, 3)
            self.assertEqual(resumed[-1]['downloaded_bytes'], 7)
            self.assertEqual(resumed[-1]['total_bytes'], 7)
            self.assertIsNone(resumed[-1]['bytes_per_second'])
