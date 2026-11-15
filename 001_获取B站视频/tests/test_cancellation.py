"""验证真实信号、阻塞网络及媒体进程的完整退出，并检查日志不再更新。"""

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import shutil
import ssl
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch, MagicMock

from bilibili_downloader import config, runtime
from bilibili_downloader.activity_log import HtmlLog
from bilibili_downloader.errors import DownloadCancelled
from tests import PROJECT_DIR


def process_alive(pid: int) -> bool:
    """只读判断 pid 是否仍在运行；Windows 使用进程查询句柄，其他系统用信号 0，返回布尔值。"""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class CancellationTests(unittest.TestCase):
    """所有取消实验运行在独立进程和临时目录中，不触碰用户进程及媒体。"""

    def setUp(self) -> None:
        """创建隔离目录和配置；无参数及返回值。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = replace(config.load_settings(PROJECT_DIR / 'config.yaml'), output_dir=self.root)

    def run_worker(self, mode: str, url: str = 'http://127.0.0.1/') -> None:
        """启动 mode/URL 场景，发送中断并断言进程、日志及媒体子进程均正确收尾；无返回值。"""
        env = dict(os.environ, PYTHONUTF8='1')
        process = subprocess.Popen([sys.executable, str(PROJECT_DIR / 'tests' / 'cancellation_worker.py'),
                                    str(self.root), mode, url], cwd=PROJECT_DIR, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                   creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
        try:
            marker = self.root / ('ready' if mode == 'network' else 'media.pid')
            deadline = time.monotonic() + 20
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if not marker.exists():
                stdout, stderr = process.communicate(timeout=5)
                self.fail(f'未进入测试阶段：{stdout}\n{stderr}')
            started = time.monotonic()
            if mode != 'sigint':
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)
                if process.poll() is None:
                    process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                self.fail(f'中断后未退出：{stdout}\n{stderr}')
            self.assertLess(time.monotonic() - started, 8, stdout + stderr)
            self.assertEqual(process.returncode, 130, stdout + stderr)
            self.assertIn('已中止', stderr)
            self.assertEqual(json.loads((self.root / 'remaining_threads.json').read_text(encoding='utf-8')), [])
            state = self.root / self.settings.log_state_filename
            data = json.loads(state.read_text(encoding='utf-8'))
            self.assertTrue(data['closed'])
            self.assertEqual(data['runs'][-1]['status'], 'interrupted')
            self.assertTrue(all(entry['status'] in ('interrupted', 'skipped') for entry in data['runs'][-1]['entries']))
            previous = state.stat().st_mtime_ns
            time.sleep(0.15)
            self.assertEqual(state.stat().st_mtime_ns, previous)
            if (self.root / 'media.pid').exists():
                self.assertFalse(process_alive(int((self.root / 'media.pid').read_text())))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_console_interrupt_aborts_blocked_http_and_preserves_partial(self) -> None:
        """验证控制台事件可中断已建立但阻塞的 HTTP 读取，保留已写断点；无返回值。"""
        self.run_blocked_server(tls=False)

    def test_console_interrupt_aborts_blocked_https_and_preserves_partial(self) -> None:
        """验证实际使用的 TLS 套接字也能取消，证书仅用于本地测试；无返回值。"""
        self.run_blocked_server(tls=True)

    def run_blocked_server(self, *, tls: bool) -> None:
        """启动 tls 指定的阻塞 HTTP/HTTPS 服务器，运行取消断点场景并关闭服务器；无返回值。"""
        stop = Event()

        class Handler(BaseHTTPRequestHandler):
            """提供一个首块后持续阻塞的本地响应。"""

            def do_GET(self) -> None:
                """写入首块后等待 stop；模拟网络停滞，无参数及返回值。"""
                self.send_response(200)
                self.send_header('Content-Length', '1000000')
                self.end_headers()
                self.wfile.write(b'x' * 1024)
                self.wfile.flush()
                stop.wait(15)

            def log_message(self, format: str, *args: object) -> None:
                """忽略 format/args 访问日志；无返回值。"""

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        if tls:
            executable = shutil.which('openssl')
            if executable is None:
                server.server_close()
                self.skipTest('TLS 取消用例需要 OpenSSL 生成临时证书')
            key, certificate = self.root / 'key.pem', self.root / 'certificate.pem'
            openssl_config = self.root / 'openssl.cnf'
            openssl_config.write_text('[req]\ndistinguished_name=dn\n[dn]\n', encoding='utf-8')
            result = subprocess.run([executable, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                                     '-config', str(openssl_config),
                                     '-keyout', str(key), '-out', str(certificate), '-days', '1',
                                     '-subj', '/CN=localhost'], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            self.run_worker('network', f'{"https" if tls else "http"}://127.0.0.1:{server.server_port}/video')
            self.assertEqual((self.root / 'video.part').read_bytes(), b'x' * 1024)
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_console_interrupt_reaps_running_media_child(self) -> None:
        """验证外部控制台事件结束运行中的媒体进程，不留下工作线程；无返回值。"""
        self.run_worker('media')

    def test_sigint_and_repeated_sigint_do_not_interrupt_cleanup(self) -> None:
        """验证真实 SIGINT 及重复 SIGINT 沿同一取消路径执行，清理不会被打断；无返回值。"""
        self.run_worker('sigint')

    def test_cancelled_process_escalates_to_kill_when_terminate_times_out(self) -> None:
        """验证媒体进程拒绝退出时，配置的等待超时后强制回收；无返回值。"""
        process = MagicMock()
        process.poll.return_value = None
        controller = runtime.Cancellation(self.settings)

        def communicate(*args: object, **kwargs: object) -> tuple[str, str]:
            """按 args/kwargs 模拟首次轮询触发取消、第二次终止超时、第三次回收，返回文本输出。"""
            controller.event.set()
            if process.communicate.call_count < 3:
                raise subprocess.TimeoutExpired('media', 0.1)
            return '', ''

        process.communicate.side_effect = communicate
        with runtime.bind(controller), patch.object(subprocess, 'Popen', return_value=process), self.assertRaises(DownloadCancelled):
            runtime.run_process(['media'], capture_output=True, text=True)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.call_count, 3)

    def test_progress_callback_drains_both_pipes_before_process_exit(self) -> None:
        """验证实时回调在子进程退出前触发，大量 stderr 不阻塞 stdout，取消模式仍回收媒体进程；无返回值。"""
        observed = []
        controller = runtime.Cancellation(self.settings)

        def progress(line: str) -> None:
            """记录 line；收到开始标记后请求取消，验证实时回调而非进程结束后解析；无返回值。"""
            observed.append(line.strip())
            controller.event.set()

        command = [sys.executable, '-u', '-c', "import sys,time; sys.stderr.write('x'*200000); sys.stderr.flush(); print('progress=continue',flush=True); time.sleep(30)"]
        with runtime.bind(controller), self.assertRaises(DownloadCancelled):
            runtime.run_process(command, stdout_callback=progress, capture_output=True, text=True)
        self.assertEqual(observed, ['progress=continue'])
