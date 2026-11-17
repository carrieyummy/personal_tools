"""独立调度媒体下载，解析、续签与后处理共用处理池，原始分片字节队列有界。"""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Empty, SimpleQueue
from threading import Event
from typing import Any, Callable, Iterable, TypeVar

from .client import check_cancel
from .errors import AuthenticationError, Cancelled, ToolError


Job = TypeVar("Job")
Transfer = TypeVar("Transfer")


def run_pipeline(jobs: Iterable[Job], config: dict[str, Any], steps: Callable[[Job], Any],
                 complete: Callable[[Job, str | None, Exception | None], None],
                 cancel: Event, auth_stop: Event, *, downloaded_size: Callable[[Transfer], int],
                 waiting: Callable[[Job, str], None]) -> None:
    """按 jobs 顺序调度 steps(job) 阶段生成器；config 独立限制下载及共享处理，cancel/auth_stop 控制取消和认证停止。

    steps 让出 download/resolve/ready 及载荷，分别切换下载池、共享处理池、媒体队列，最终返回终态字符串。
    complete 在调用线程接收任务、终态或异常，waiting 接收任务及等待阶段；回调均无返回值。
    downloaded_size 返回载荷的原始分片正整数字节数；仅 ready 占容量，单集超限可独占空队列。
    解析和等待入队不占下载名额；容量不足暂停新下载，预解析缓存有界；jobs 按需展开。
    取消或批次异常时回收两个池并结算已接纳任务；已完成的终态保留，其余记录取消，无返回值。
    """
    source = iter(jobs)
    prepared, resolving = deque(), deque()
    admission: deque[tuple[Job, Transfer, int]] = deque()
    ready: deque[tuple[Job, Transfer, int]] = deque()
    queued_bytes, exhausted = 0, False
    capacity = int(config["download"]["queue_capacity_gb"] * 1024**3)
    download_limit = config["download"]["threads"]
    process_limit = config["max_in_flight"]
    downloads = ThreadPoolExecutor(max_workers=download_limit, thread_name_prefix="mgtv-download")
    processing = ThreadPoolExecutor(max_workers=process_limit, thread_name_prefix="mgtv-process")
    active_downloads: dict[Future, Job] = {}
    active_processing: dict[Future, Job] = {}
    started: dict[int, Job] = {}
    finished: SimpleQueue[Future] = SimpleQueue()
    generators: dict[int, Any] = {}

    def advance(job: Job):
        """推进 job 的阶段生成器；首次在处理池创建，返回阶段元组或终态标记。"""
        check_cancel(cancel)
        generator = generators.setdefault(id(job), None)
        if generator is None:
            generator = generators[id(job)] = steps(job)
        try:
            return next(generator)
        except StopIteration as done:
            return "done", done.value

    def submit(pool: ThreadPoolExecutor, active: dict[Future, Job], job: Job, phase: str) -> None:
        """向 pool 提交 job 的阶段推进，记录 phase、active 及完成通知；无返回值。"""
        future = pool.submit(advance, job)
        future.phase = phase
        active[future] = job
        future.add_done_callback(finished.put)

    def settle(job: Job, status: str | None, error: Exception | None) -> None:
        """结算 job 的 status/error 并移除接纳登记；回调在调用线程执行，无返回值。"""
        started.pop(id(job), None)
        generator = generators.pop(id(job), None)
        if generator is not None:
            generator.close()
        complete(job, status, error)

    try:
        while not exhausted or prepared or resolving or admission or ready or active_downloads or active_processing:
            check_cancel(cancel)
            if auth_stop.is_set():
                exhausted = True
            while admission and (queued_bytes + admission[0][2] <= capacity or not ready):
                job, transferred, size = admission.popleft()
                ready.append((job, transferred, size))
                queued_bytes += size
                waiting(job, "等待后处理")
            while len(active_processing) < process_limit:
                check_cancel(cancel)
                if ready:
                    job, transferred, size = ready.popleft()
                    queued_bytes -= size
                    phase = "processing"
                elif resolving:
                    job = resolving.popleft()
                    phase = "resolving"
                elif not exhausted and not admission:
                    preparing = sum(future.phase == "initial" for future in active_processing)
                    if len(prepared) + preparing >= download_limit:
                        break
                    try:
                        job = next(source)
                    except StopIteration:
                        exhausted = True
                        break
                    if auth_stop.is_set():
                        exhausted = True
                        break
                    started[id(job)] = job
                    phase = "initial"
                else:
                    break
                submit(processing, active_processing, job, phase)
            while prepared and not admission and len(active_downloads) < download_limit:
                check_cancel(cancel)
                job = prepared.popleft()
                if auth_stop.is_set():
                    settle(job, None, AuthenticationError("登录失效，未继续下载"))
                else:
                    submit(downloads, active_downloads, job, "download")
            # 出队释放的容量在下一轮立刻用于入队，避免没有 Future 时等待空通知。
            if (not active_downloads and not active_processing
                    or admission and (queued_bytes + admission[0][2] <= capacity or not ready)):
                continue
            try:
                future = finished.get(timeout=config["runtime"]["cancel_poll_interval_seconds"])
            except Empty:
                continue
            is_download = future in active_downloads
            job = active_downloads.pop(future) if is_download else active_processing.pop(future)
            try:
                kind, value = future.result()
                if kind == "ready":
                    size = downloaded_size(value)
                    if type(size) is not int or size <= 0:
                        raise ToolError("下载证据中的原始分片字节数必须为正整数")
            except Exception as error:
                settle(job, None, error)
            else:
                if kind == "ready":
                    admission.append((job, value, size))
                    waiting(job, "等待队列容量")
                elif kind == "download":
                    prepared.append(job)
                    waiting(job, "等待下载")
                elif kind == "resolve":
                    resolving.append(job)
                    waiting(job, "等待处理")
                elif kind == "done":
                    settle(job, value, None)
                else:
                    settle(job, None, ToolError("未知流水线阶段"))
        # 最后一个任务完成后仍检查取消，保留成功终态并让批次返回取消结果。
        check_cancel(cancel)
    except BaseException as error:
        cancel.set()
        for future in (*active_downloads, *active_processing):
            future.cancel()
        downloads.shutdown(wait=True, cancel_futures=True)
        processing.shutdown(wait=True, cancel_futures=True)
        # 不把取消前已经发布成功、复用或失败的任务覆盖成取消。
        outcomes: dict[int, tuple[str | None, Exception | None]] = {}
        for future, job in (*active_downloads.items(), *active_processing.items()):
            if future.cancelled():
                continue
            try:
                kind, value = future.result()
            except Exception as task_error:
                outcomes[id(job)] = (None, task_error)
            except BaseException:
                continue
            else:
                if kind == "done":
                    outcomes[id(job)] = (value, None)
        for job in list(started.values()):
            try:
                status, task_error = outcomes.get(id(job), (None, Cancelled("现场已保留")))
                settle(job, status, task_error)
            except BaseException as cleanup_error:
                error.add_note(f"任务终态保存失败：{cleanup_error}")
        raise
    finally:
        downloads.shutdown(wait=True, cancel_futures=True)
        processing.shutdown(wait=True, cancel_futures=True)
