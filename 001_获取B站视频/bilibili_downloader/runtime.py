"""统一管理运行取消、活动 HTTP 响应及可取消媒体子进程，不强行终止工作线程。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import signal
import socket
import subprocess
import sys
from threading import Event, Lock, Thread, current_thread, main_thread
from typing import Any, Callable, Iterator

from .config import Settings
from .errors import DownloadCancelled, check_cancel, current_cancel_event


_controller: ContextVar[Cancellation | None] = ContextVar('bilibili_cancellation', default=None)


def current() -> Cancellation | None:
    """返回当前线程绑定的取消控制器，未绑定时返回 None；无参数。"""
    return _controller.get()


@contextmanager
def bind(controller: Cancellation | None) -> Iterator[None]:
    """将 controller 绑定到当前线程，并在退出时恢复原上下文；产生 None。"""
    token = _controller.set(controller)
    event_token = current_cancel_event.set(controller.event if controller else None)
    try:
        yield
    finally:
        current_cancel_event.reset(event_token)
        _controller.reset(token)


def run_bound(controller: Cancellation | None, function: Callable, *args: Any, **kwargs: Any) -> Any:
    """在 controller 上下文内调用 function，透传 args/kwargs 并返回调用结果。"""
    with bind(controller):
        return function(*args, **kwargs)


def abort_socket(connection: socket.socket) -> None:
    """中断本工具拥有的 connection 并立即关闭底层句柄，唤醒 Windows 缓冲读取；无返回值。"""
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    # makefile 的 SocketIO 引用会使普通 close 延后；detach 后显式关闭，避免等待缓冲流释放引用。
    try:
        descriptor = connection.detach()
        if descriptor >= 0:
            socket.close(descriptor)
    except OSError:
        pass


class Cancellation:
    """一次运行的取消资源所有者；共享 event，并在独立线程关闭活动网络响应。"""

    def __init__(self, settings: Settings) -> None:
        """按 settings 保存轮询和进程终止设置，创建取消事件；无返回值。"""
        self.settings = settings
        self.event, self.wake = Event(), Event()
        self.lock = Lock()
        self.responses: dict[int, Any] = {}
        self.handlers: dict[int, Any] = {}
        self.watcher = Thread(target=self._watch, name='bilibili-cancel')

    def __enter__(self) -> Cancellation:
        """绑定上下文并在主线程安装控制台中断处理器；返回当前控制器。"""
        self.scope = bind(self)
        self.scope.__enter__()
        if current_thread() is main_thread():
            for sig in (signal.SIGINT, *([signal.SIGBREAK] if hasattr(signal, 'SIGBREAK') else [])):
                self.handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._signal)
        self.watcher.start()
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> None:
        """按 exc_type/exc/traceback 请求取消或正常关闭，等待资源看护线程并恢复信号；不吞异常。"""
        try:
            if isinstance(exc, KeyboardInterrupt):
                self.cancel()
            self.wake.set()
            self.watcher.join()
            with self.lock:
                responses = list(self.responses.values())
            for response in responses:
                try:
                    response.close()
                except (OSError, ValueError):
                    pass
        finally:
            for sig, handler in self.handlers.items():
                signal.signal(sig, handler)
            self.scope.__exit__(exc_type, exc, traceback)
        if self.event.is_set() and exc is None:
            raise DownloadCancelled('用户已取消下载')

    def _signal(self, signum: int, frame: Any) -> None:
        """响应 signum/frame 控制台信号，只设置标志；重复中断不向清理代码注入异常。"""
        self.cancel()

    def cancel(self) -> None:
        """幂等请求取消并唤醒网络清理线程；无参数及返回值。"""
        self.event.set()
        self.wake.set()

    def _watch(self) -> None:
        """等待取消并关闭已建立响应的底层套接字；不持资源锁执行关闭，无参数及返回值。"""
        self.wake.wait()
        if not self.event.is_set():
            return
        print('\n正在停止并保存断点，请等待资源清理完成……', file=sys.stderr, flush=True)
        with self.lock:
            responses = list(self.responses.values())
        for response in responses:
            self._abort_response(response)

    def _abort_response(self, response: Any) -> None:
        """中断 response 的套接字读取，再由任务正常关闭响应；兼容 yt-dlp 各标准 HTTP 适配层，无返回值。"""
        # 请求库的适配层不同，但最终均持有 socket；先 shutdown 避免另一线程的缓冲读取锁阻塞 close。
        pending, seen = [response], set()
        while pending:
            obj = pending.pop()
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            if isinstance(obj, socket.socket):
                abort_socket(obj)
                continue
            for name in ('fp', 'raw', '_fp', '_sock', 'sock', '_connection'):
                child = getattr(obj, name, None)
                if child is not None:
                    pending.append(child)

    def track(self, response: Any) -> Any:
        """登记 response，并在 read/close 边界检查取消及释放登记；返回原响应对象，保持下载器接口。"""
        check_cancel(self.event)
        read, close = response.read, response.close
        with self.lock:
            self.responses[id(response)] = response

        def read_checked(*args: Any, **kwargs: Any) -> bytes:
            """按 args/kwargs 读取响应；取消引起的 I/O 错误转换为取消异常，返回响应字节。"""
            check_cancel(self.event)
            try:
                result = read(*args, **kwargs)
            except Exception:
                check_cancel(self.event)
                raise
            check_cancel(self.event)
            return result

        def close_tracked() -> None:
            """关闭当前响应并撤销登记；无参数及返回值。"""
            try:
                close()
            finally:
                with self.lock:
                    self.responses.pop(id(response), None)

        response.read, response.close = read_checked, close_tracked
        if self.event.is_set():
            self._abort_response(response)
            close_tracked()
            check_cancel(self.event)
        return response


def attach_downloader(downloader: Any) -> None:
    """为 downloader 的 urlopen 登记当前运行的响应；没有控制器时保持原接口，无返回值。"""
    controller = current()
    if controller is None:
        return
    original = downloader.urlopen

    def open_checked(*args: Any, **kwargs: Any) -> Any:
        """以 args/kwargs 发出请求并登记响应；连接阶段取消受原有超时限制，返回响应对象。"""
        check_cancel(controller.event)
        try:
            response = original(*args, **kwargs)
        except Exception:
            check_cancel(controller.event)
            raise
        if controller.event.is_set():
            response.close()
            check_cancel(controller.event)
        return controller.track(response)

    downloader.urlopen = open_checked


def run_process(command: list[str], *, stdout_callback: Callable[[str], None] | None = None,
                **options: Any) -> subprocess.CompletedProcess:
    """执行 command 并返回 CompletedProcess；options 为 subprocess 选项，stdout_callback 可按行接收文本进度。

    进度模式同时排空标准输出和错误流；所有模式取消时均终止并回收子进程。
    """
    controller = current()
    if stdout_callback is not None:
        return run_streaming_process(command, stdout_callback, controller, options)
    if controller is None:
        return subprocess.run(command, **options)
    check_cancel()
    capture = options.pop('capture_output', False)
    if capture:
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process = subprocess.Popen(command, **options)
    try:
        while True:
            check_cancel()
            try:
                stdout, stderr = process.communicate(timeout=controller.settings.cancel_poll_interval)
                check_cancel()
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=controller.settings.process_terminate_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise


def run_streaming_process(command: list[str], callback: Callable[[str], None], controller: Cancellation | None,
                          options: dict[str, Any]) -> subprocess.CompletedProcess:
    """按 options 运行 command，逐行交给 callback，controller 控制取消；返回捕获完整文本的 CompletedProcess。"""
    check_cancel()
    options = dict(options)
    options.pop('capture_output', None)
    options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process = subprocess.Popen(command, **options)
    output, errors, callback_errors = [], [], []

    def read_output() -> None:
        """持续排空标准输出并调用进度回调；回调错误交由主线程回收进程后抛出，无参数及返回值。"""
        try:
            for line in process.stdout:
                output.append(line)
                if not callback_errors:
                    try:
                        callback(line)
                    except Exception as exc:
                        callback_errors.append(exc)
        except Exception as exc:
            callback_errors.append(exc)

    def read_errors() -> None:
        """持续排空错误流，避免 FFmpeg 日志写满管道；无参数及返回值。"""
        try:
            errors.append(process.stderr.read())
        except Exception as exc:
            callback_errors.append(exc)

    readers = [Thread(target=read_output, name='media-progress'), Thread(target=read_errors, name='media-errors')]
    for reader in readers:
        reader.start()
    try:
        while True:
            check_cancel()
            if callback_errors:
                raise callback_errors[0]
            try:
                process.wait(timeout=controller.settings.cancel_poll_interval if controller else None)
                check_cancel()
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=controller.settings.process_terminate_timeout if controller else None)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        for reader in readers:
            reader.join()
        process.stdout.close()
        process.stderr.close()
    if callback_errors:
        raise callback_errors[0]
    return subprocess.CompletedProcess(command, process.returncode, ''.join(output), ''.join(errors))
