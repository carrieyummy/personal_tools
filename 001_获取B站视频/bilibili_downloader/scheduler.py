"""提供顺序补位的下载与后处理池，按原始流字节限制等待队列并协作取消。"""

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from queue import Empty, SimpleQueue
from threading import Event
from typing import Callable, TypeVar

from .config import Settings
from .errors import DownloadCancelled, check_cancel
from . import runtime


Job = TypeVar('Job')
Payload = TypeVar('Payload')
Result = TypeVar('Result')
Transfer = TypeVar('Transfer')


@dataclass
class PostTask:
    """后处理附加任务；execute 为无参数处理函数，complete 在主线程接收可选异常，两者均无返回值。"""

    execute: Callable[[], None]
    complete: Callable[[BaseException | None], None]


def run_pipeline(jobs: list[Job], settings: Settings, prepare: Callable[[Job], Payload | None],
                 download: Callable[[Payload, Event], Transfer], process: Callable[[Transfer, Event], Result],
                 complete: Callable[[Job, Result | None, BaseException | None], None], cancel_event: Event, *,
                 downloaded_size: Callable[[Transfer], int], waiting: Callable[[Job, str], None],
                 finishers: deque[PostTask]) -> None:
    """按 jobs 顺序独立调度下载及后处理，settings 提供两池上限、字节容量和取消轮询间隔。

    prepare/complete/waiting 在调用线程执行；download/process 在独立池接收共享 cancel_event。
    downloaded_size 返回完整原始流字节数，waiting 接收等待容量或后处理的状态键；prepare 可返回 None。
    finishers 可由准备或完成回调追加，共用后处理池并优先派发；无返回值。
    容量仅统计 ready；下载中与等待入队共用下载名额，单个超容量任务允许独占空队列。
    """
    pending = deque(jobs)
    admission: deque[tuple[Job, Transfer, int]] = deque()
    ready: deque[tuple[Job, Transfer, int]] = deque()
    queued_bytes = 0
    capacity = int(settings.queue_capacity_gb * 1024**3)
    controller = runtime.current()
    downloads = ThreadPoolExecutor(max_workers=settings.threads, thread_name_prefix='bilibili-download')
    processing = ThreadPoolExecutor(max_workers=settings.max_in_flight, thread_name_prefix='bilibili-process')
    active_downloads: dict[Future, Job] = {}
    active_processing: dict[Future, Job | PostTask] = {}
    started: dict[int, Job] = {}
    finished: SimpleQueue[Future] = SimpleQueue()

    def submit(pool: ThreadPoolExecutor, active: dict, owner: Job | PostTask, function: Callable, *args) -> None:
        """将 owner 的 function/args 绑定当前运行后交给 pool，并登记 active；无返回值。"""
        future = pool.submit(runtime.run_bound, controller, function, *args)
        active[future] = owner
        future.add_done_callback(finished.put)

    def settle(job: Job, result: Result | None, error: BaseException | None) -> None:
        """在调用线程交付 job 的 result/error，并移除取消清理登记；无返回值。"""
        complete(job, result, error)
        started.pop(id(job), None)

    try:
        while pending or admission or ready or active_downloads or active_processing or finishers:
            check_cancel(cancel_event)
            while admission and (queued_bytes + admission[0][2] <= capacity or not ready):
                job, transferred, size = admission.popleft()
                ready.append((job, transferred, size))
                queued_bytes += size
                waiting(job, 'waiting_processing')
            while len(active_processing) < settings.max_in_flight and (finishers or ready):
                check_cancel(cancel_event)
                if finishers:
                    task = finishers.popleft()
                    submit(processing, active_processing, task, task.execute)
                else:
                    job, transferred, size = ready.popleft()
                    queued_bytes -= size
                    submit(processing, active_processing, job, process, transferred, cancel_event)
            while pending and len(active_downloads) + len(admission) < settings.threads:
                check_cancel(cancel_event)
                job = pending.popleft()
                payload = prepare(job)
                if payload is not None:
                    started[id(job)] = job
                    check_cancel(cancel_event)
                    submit(downloads, active_downloads, job, download, payload, cancel_event)
            # prepare 可能追加纯收尾任务；没有 Future 时立刻回到派发，避免空队列等待。
            if (not active_downloads and not active_processing
                    or admission and (queued_bytes + admission[0][2] <= capacity or not ready)
                    or (finishers or ready) and len(active_processing) < settings.max_in_flight):
                continue
            try:
                future = finished.get(timeout=settings.cancel_poll_interval)
            except Empty:
                continue
            is_download = future in active_downloads
            owner = active_downloads.pop(future) if is_download else active_processing.pop(future)
            try:
                value = future.result()
                size = downloaded_size(value) if is_download else 0
            except (KeyboardInterrupt, DownloadCancelled):
                raise
            except Exception as error:
                if isinstance(owner, PostTask):
                    owner.complete(error)
                else:
                    settle(owner, None, error)
            else:
                if is_download:
                    admission.append((owner, value, size))
                    waiting(owner, 'waiting_queue')
                elif isinstance(owner, PostTask):
                    owner.complete(None)
                else:
                    settle(owner, value, None)
    except BaseException as error:
        cancel_event.set()
        if controller:
            controller.cancel()
        for future in list(active_downloads) + list(active_processing):
            future.cancel()
        downloads.shutdown(wait=True, cancel_futures=True)
        processing.shutdown(wait=True, cancel_futures=True)
        interruption = DownloadCancelled('用户取消下载，断点已保留')
        for job in list(started.values()):
            try:
                settle(job, None, interruption)
            except BaseException as cleanup_error:
                error.add_note(f'任务中断状态保存失败：{cleanup_error}')
        raise
    finally:
        downloads.shutdown(wait=True, cancel_futures=True)
        processing.shutdown(wait=True, cancel_futures=True)


def run_jobs(jobs: list[Job], threads: int, prepare: Callable[[Job], Payload | None],
             execute: Callable[[Payload, Event], Result],
             complete: Callable[[Job, Result | None, BaseException | None], None],
             cancel_event: Event) -> None:
    """按 jobs 顺序派发，最多 threads 个任务；prepare 返回 None 时不占线程。

    prepare/complete 在主线程准备参数及处理结果，execute 在独立线程执行并接收 cancel_event。
    完成通知按实际入队顺序处理，避免一次收到多个完成结果时按无序集合触发投稿合并。
    异常或取消时停止派发、取消未启动任务并等待运行中任务保留断点后退出，无返回值。
    """
    if type(threads) is not int or threads < 1:
        raise ValueError('线程数必须为正整数')
    queue = deque(jobs)
    executor = ThreadPoolExecutor(max_workers=threads, thread_name_prefix='bilibili')
    futures: dict[Future, Job] = {}
    finished: SimpleQueue[Future] = SimpleQueue()
    try:
        while queue or futures:
            check_cancel(cancel_event)
            while queue and len(futures) < threads:
                check_cancel(cancel_event)
                job = queue.popleft()
                payload = prepare(job)
                check_cancel(cancel_event)
                if payload is not None:
                    future = executor.submit(execute, payload, cancel_event)
                    futures[future] = job
                    future.add_done_callback(finished.put)
            if futures:
                try:
                    future = finished.get(timeout=0.1)
                except Empty:
                    continue
                job = futures.pop(future)
                try:
                    result = future.result()
                except BaseException as exc:
                    complete(job, None, exc)
                else:
                    complete(job, result, None)
    except BaseException:
        cancel_event.set()
        controller = runtime.current()
        if controller:
            controller.cancel()
        for future in futures:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
