"""验证 URL 分片下载的实际字节、续签和断点复用统计。"""

from copy import deepcopy
from io import BytesIO
from pathlib import Path
import tempfile
from threading import Event
import unittest

import yaml

from local.playback.errors import AddressExpired
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
