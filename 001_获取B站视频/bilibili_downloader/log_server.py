"""提供回环地址日志服务与网页终态确认；浏览器读取内存，不改写持久化日志。"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
from threading import Condition, Lock, Thread, current_thread
import time
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import uuid4

from .errors import ToolError
from .storage import validate_path
from .runtime import abort_socket


class LogServer:
    """一次运行的本地日志服务器；journal 提供快照及媒体路径，近期活跃网页逐页确认终态。"""

    def __init__(self, journal: Any) -> None:
        """为 journal 绑定配置端口并启动后台服务；无返回值，仅监听 127.0.0.1。"""
        self.journal, self.lock = journal, Lock()
        self.closing = False
        self.connections: dict[Any, Thread] = {}
        self.condition = Condition(self.lock)
        self.clients: dict[str, float] = {}
        self.final_receipts: dict[str, int] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            """读取 owner 的页面、状态、媒体，并确认网页已显示的最终版本。"""

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
                self.wfile.flush()

            def do_GET(self) -> None:
                """处理 GET 读取及终态确认；拒绝外部 Host、非法确认及越界媒体，无参数及返回值。"""
                if self.headers.get('Host') != urlsplit(owner.url).netloc:
                    self.send_error(403)
                    return
                parsed = urlsplit(self.path)
                route, query = parsed.path, parse_qs(parsed.query)
                client_id = query.get('client_id', [''])[0]
                try:
                    if route == '/':
                        client_id = uuid4().hex
                        owner.register_client(client_id)
                        self.send_bytes(owner.journal.render_document(live=True, client_id=client_id).encode('utf-8'),
                                        'text/html; charset=utf-8')
                    elif route == '/api/state':
                        if client_id:
                            if not re.fullmatch(r'[0-9a-f]{32}', client_id):
                                self.send_error(400)
                                return
                            owner.register_client(client_id)
                        body = json.dumps(owner.journal.snapshot(live=True), ensure_ascii=False).encode('utf-8')
                        self.send_bytes(body, 'application/json; charset=utf-8')
                    elif route == '/api/ack':
                        run_id = query.get('run_id', [''])[0]
                        revision = query.get('revision', [''])[0]
                        with owner.journal.lock:
                            valid = (owner.journal.closed and run_id == owner.journal.run['id']
                                     and revision == str(owner.journal.data['revision']))
                        with owner.lock:
                            valid = valid and client_id in owner.clients
                        if not valid:
                            self.send_error(409)
                            return
                        # 确认响应先写完再唤醒关闭线程，避免关闭过程截断当前响应。
                        self.send_bytes(b'{}', 'application/json; charset=utf-8')
                        with owner.condition:
                            owner.final_receipts[client_id] = int(revision)
                            owner.condition.notify_all()
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

    def register_client(self, client_id: str) -> None:
        """登记 client_id 网页最近一次请求的单调时间；标识由服务生成或经接口格式校验，无返回值。"""
        with self.condition:
            self.clients[client_id] = time.monotonic()
            self.condition.notify_all()

    def wait_for_final_state(self) -> bool:
        """等待近期活跃网页逐页确认当前最终版本；无参数，全部确认或无活跃网页返回 True，超时返回 False。"""
        with self.journal.lock:
            revision = self.journal.data['revision']
        timeout = self.journal.settings.log_final_state_timeout
        started = time.monotonic()
        deadline = started + timeout
        # 已长期停止轮询的历史页面不再参与；刚打开尚未首次轮询的页面由首页请求登记。
        cutoff = started - max(timeout, self.journal.settings.log_refresh_interval)
        with self.condition:
            while any(seen >= cutoff and self.final_receipts.get(client_id) != revision
                      for client_id, seen in self.clients.items()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
        return True

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
