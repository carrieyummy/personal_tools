"""提供只监听回环地址的只读日志服务，浏览器请求不读取持久化日志文件。"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from threading import Lock, Thread, current_thread
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .errors import ToolError
from .storage import validate_path
from .runtime import abort_socket


class LogServer:
    """一次运行的本地日志服务器；journal 提供内存快照、页面和受限媒体路径。"""

    def __init__(self, journal: Any) -> None:
        """为 journal 绑定配置端口并启动后台服务；无返回值，仅监听 127.0.0.1。"""
        self.journal, self.lock = journal, Lock()
        self.closing = False
        self.connections: dict[Any, Thread] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            """只读访问当前 owner 的页面、内存状态、快照和已完成媒体。"""

            def setup(self) -> None:
                """为当前 HTTP 连接设置超时并登记线程；无参数及返回值。"""
                self.request.settimeout(owner.journal.settings.request_timeout)
                super().setup()
                with owner.lock:
                    owner.connections[self.request] = current_thread()

            def finish(self) -> None:
                """完成请求并撤销连接登记；无参数及返回值。"""
                try:
                    super().finish()
                finally:
                    with owner.lock:
                        owner.connections.pop(self.request, None)

            def log_message(self, format: str, *args: Any) -> None:
                """忽略 format/args 描述的访问日志，避免轮询刷屏；无返回值。"""

            def send_bytes(self, body: bytes, content_type: str, *, filename: str = '') -> None:
                """发送 body、content_type，filename 非空时作为下载附件；禁用缓存，无返回值。"""
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                if filename:
                    self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                """处理只读 GET 请求；拒绝外部 Host、目录遍历及未完成媒体，无参数及返回值。"""
                if self.headers.get('Host') != urlsplit(owner.url).netloc:
                    self.send_error(403)
                    return
                route = urlsplit(self.path).path
                try:
                    if route == '/':
                        self.send_bytes(owner.journal.render_document(live=True).encode('utf-8'), 'text/html; charset=utf-8')
                    elif route == '/api/state':
                        body = json.dumps(owner.journal.snapshot(live=True), ensure_ascii=False).encode('utf-8')
                        self.send_bytes(body, 'application/json; charset=utf-8')
                    elif route == '/snapshot':
                        self.send_bytes(owner.journal.render_document(live=False).encode('utf-8'),
                                        'text/html; charset=utf-8', filename=owner.journal.snapshot_filename())
                    elif route.startswith('/files/'):
                        self.send_media(unquote(route.removeprefix('/files/')))
                    else:
                        self.send_error(404)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                    return

            def send_media(self, relative: str) -> None:
                """发送 relative 对应且已登记完成的媒体；路径限定在输出目录，无返回值。"""
                root = owner.journal.settings.output_dir.resolve()
                target = root / relative
                try:
                    validate_path(target, root)
                    target = target.resolve()
                    data = owner.journal.snapshot(live=True)
                    allowed = {Path(entry['path']).resolve() for run in data['runs'] for entry in run['entries']
                               if entry.get('path') and entry['status'] in ('completed', 'reused', 'warning')}
                    if target not in allowed or not target.is_file():
                        self.send_error(404)
                        return
                    with target.open('rb') as stream:
                        self.send_response(200)
                        self.send_header('Content-Type', mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
                        self.send_header('Content-Length', str(target.stat().st_size))
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        while chunk := stream.read(64 * 1024):
                            self.wfile.write(chunk)
                except (OSError, ValueError, ToolError):
                    if not self.wfile.closed:
                        self.send_error(404)

        class Server(ThreadingHTTPServer):
            """在正常关闭连接时抑制预期的套接字异常，运行中错误仍按标准服务记录。"""

            def handle_error(self, request: Any, client_address: Any) -> None:
                """处理 request/client_address 的请求异常；关闭阶段不打印预期断开，无返回值。"""
                if not owner.closing:
                    super().handle_error(request, client_address)

        self.http = Server(('127.0.0.1', journal.settings.log_port), Handler)
        self.url = f'http://127.0.0.1:{self.http.server_port}/'
        self.thread = Thread(target=self.http.serve_forever,
                             kwargs={'poll_interval': journal.settings.cancel_poll_interval}, name='bilibili-log')
        self.thread.start()

    def close(self) -> None:
        """停止接受请求，断开现有连接并等待服务线程退出；无参数及返回值。"""
        self.closing = True
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        with self.lock:
            connections = list(self.connections.items())
        for connection, thread in connections:
            abort_socket(connection)
        for connection, thread in connections:
            thread.join()
