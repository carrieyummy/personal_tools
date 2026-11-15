"""验证 HTTP 分块配置，以及服务端截断大响应时的真实分块续传；仅使用本地 HTTP 服务。"""

from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import tempfile
from threading import Thread
import unittest

import yaml
from yt_dlp.utils import DownloadError

from bilibili_downloader import auth, config, downloader
from bilibili_downloader.errors import ToolError
from tests import CONFIG_PATH, fake_auth_snapshot


class HttpChunkTests(unittest.TestCase):
    """读取实际主配置，在隔离目录验证分块校验、参数传递及受限响应续传。"""

    def setUp(self) -> None:
        """创建临时目录并读取实际配置；无参数、无返回值。"""
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        self.settings = replace(config.load_settings(CONFIG_PATH), output_dir=self.root, retries=0)
        self.document = yaml.safe_load(CONFIG_PATH.read_text(encoding='utf-8-sig'))

    def test_chunk_size_configuration_validation(self) -> None:
        """验证实际配置值、关闭分块及自定义整数可读取，非法值和缺项报错；无参数、无返回值。"""
        path = self.root / 'config.yaml'
        for value in (self.document['download']['http_chunk_size_bytes'], 0, self.settings.http_chunk_size + 1):
            with self.subTest(value=value):
                self.document['download']['http_chunk_size_bytes'] = value
                path.write_text(yaml.safe_dump(self.document), encoding='utf-8')
                self.assertEqual(config.load_settings(path).http_chunk_size, value)
        for value in (-1, True, False, 1.5, '1024', None):
            with self.subTest(invalid=value):
                self.document['download']['http_chunk_size_bytes'] = value
                path.write_text(yaml.safe_dump(self.document), encoding='utf-8')
                with self.assertRaisesRegex(ToolError, 'http_chunk_size_bytes'):
                    config.load_settings(path)
        del self.document['download']['http_chunk_size_bytes']
        path.write_text(yaml.safe_dump(self.document), encoding='utf-8')
        with self.assertRaisesRegex(ToolError, 'http_chunk_size_bytes'):
            config.load_settings(path)

    def test_downloader_uses_configured_chunk_size(self) -> None:
        """验证下载器使用配置的实际分块值，包括关闭分块；仅注入虚构认证，无参数、无返回值。"""
        for value in (self.settings.http_chunk_size, 0, self.settings.http_chunk_size + 1):
            settings = replace(self.settings, http_chunk_size=value)
            with self.subTest(value=value), auth.worker_session(fake_auth_snapshot(), settings) as session:
                with downloader.make_downloader(session, settings) as transfer:
                    self.assertEqual(transfer.params['http_chunk_size'], settings.http_chunk_size)

    def test_chunked_resume_survives_server_response_limit(self) -> None:
        """模拟大响应被截断，验证启用分块后复用失败断点并逐字节下载完整，正常分块不消耗重试。"""
        unit = b'original-encoded-stream' * 256
        response_limit = len(unit)
        payload = unit * 6 + b'end-of-stream'
        requests = []

        class LimitedRangeHandler(BaseHTTPRequestHandler):
            """声明完整请求长度，但每个响应最多发送测试流单元长度，模拟提前截断的大响应。"""

            def do_GET(self) -> None:
                """读取 Range 起止并记录请求，返回有长度上限的对应数据；无参数、无返回值。"""
                header = self.headers.get('Range')
                start_text, _, end_text = header.removeprefix('bytes=').partition('-') if header else ('0', '', '')
                start = int(start_text)
                end = min(int(end_text), len(payload) - 1) if end_text else len(payload) - 1
                requests.append((start, end))
                self.send_response(206 if header else 200)
                self.send_header('Content-Length', str(end - start + 1))
                self.send_header('Content-Type', 'application/octet-stream')
                if header:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{len(payload)}')
                self.end_headers()
                self.wfile.write(payload[start:min(end + 1, start + response_limit)])
                self.close_connection = True

            def log_message(self, format: str, *args: object) -> None:
                """忽略 format 日志模板和 args 格式化参数，保持测试输出安静；无返回值。"""

        server = ThreadingHTTPServer(('127.0.0.1', 0), LimitedRangeHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        thread.start()
        destination = self.root / 'stream.mp4'
        partial = destination.with_name(destination.name + '.part')
        info = {'id': 'stream', 'title': 'stream', 'format_id': 'raw', 'ext': 'mp4',
                'url': f'http://127.0.0.1:{server.server_port}/stream', 'protocol': 'http',
                'vcodec': 'avc1', 'acodec': 'none'}

        settings = replace(self.settings, http_chunk_size=0)
        with auth.worker_session(fake_auth_snapshot(), settings) as session:
            with downloader.make_downloader(session, settings) as transfer, redirect_stderr(io.StringIO()):
                with self.assertRaises(DownloadError):
                    transfer.dl(str(destination), info.copy())
        self.assertEqual(partial.read_bytes(), payload[:response_limit])
        resumed_at = partial.stat().st_size
        requests.clear()

        settings = replace(self.settings, http_chunk_size=response_limit)
        with auth.worker_session(fake_auth_snapshot(), settings) as session:
            with downloader.make_downloader(session, settings) as transfer:
                success, _ = transfer.dl(str(destination), info.copy())
        self.assertTrue(success)
        self.assertEqual(destination.read_bytes(), payload)
        self.assertFalse(partial.exists())
        self.assertEqual(requests[0][0], resumed_at)
        self.assertGreater(len(requests), 1)
        for index, (start, end) in enumerate(requests):
            self.assertLessEqual(end - start + 1, settings.http_chunk_size)
            if index:
                self.assertEqual(start, requests[index - 1][1] + 1)
        self.assertEqual(requests[-1][1], len(payload) - 1)
