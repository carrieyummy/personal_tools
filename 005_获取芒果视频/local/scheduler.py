"""独立调度下载和后处理，以原始分片字节限制等待队列并保留可取消的有界入队等待。"""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Empty, SimpleQueue
from threading import Event
from typing import Any, Callable, Iterable, TypeVar

from .client import check_cancel
from .errors import Cancelled, ToolError


Job = TypeVar("Job")
Transfer = TypeVar("Transfer")


def run_pipeline(jobs: Iterable[Job], config: dict[str, Any], download: Callable[[Job], Transfer | str],
                 process: Callable[[Transfer], str], complete: Callable[[Job, str | None, Exception | None], None],
                 cancel: Event, auth_stop: Event, *, downloaded_size: Callable[[Transfer], int],
                 waiting: Callable[[Job, str], None]) -> None:
    """按 jobs 的迭代顺序独立派发两阶段；config 为已校验配置，cancel/auth_stop 控制取消及停止新下载。

    download 返回后处理载荷或终态字符串，process 返回终态；两者在各自线程池执行。
    complete 在调用线程接收任务、终态或异常，waiting 接收任务及等待阶段；回调均无返回值。
    downloaded_size 返回载荷的原始分片正整数字节数；仅 ready 占容量，单集超限可独占空队列。
    下载中和 admission 中等待入队的任务共用下载名额；jobs 按需展开，不提前耗尽整个目录。
    取消或批次异常时回收两个池并结算已接纳任务；已完成的终态保留，其余记录取消，无返回值。
    """
    source = iter(jobs)
    admission: deque[tuple[Job, Transfer, int]] = deque()
    ready: deque[tuple[Job, Transfer, int]] = deque()
    queued_bytes, exhausted = 0, False
    capacity = int(config["download"]["queue_capacity_gb"] * 1024**3)
    download_limit = config["download"]["threads"]
    process_limit = config["pipeline"]["max_in_flight"]
    downloads = ThreadPoolExecutor(max_workers=download_limit, thread_name_prefix="mgtv-download")
    processing = ThreadPoolExecutor(max_workers=process_limit, thread_name_prefix="mgtv-process")
    active_downloads: dict[Future, Job] = {}
    active_processing: dict[Future, Job] = {}
    started: dict[int, Job] = {}
    finished: SimpleQueue[Future] = SimpleQueue()

    def submit(pool: ThreadPoolExecutor, active: dict[Future, Job], job: Job, function: Callable, value: Any) -> None:
        """向 pool 提交 function(value)，将所属 job 登记到 active 并订阅完成通知；无返回值。"""
        future = pool.submit(function, value)
        active[future] = job
        future.add_done_callback(finished.put)

    def settle(job: Job, status: str | None, error: Exception | None) -> None:
        """结算 job 的 status/error 并移除接纳登记；回调在调用线程执行，无返回值。"""
        started.pop(id(job), None)
        complete(job, status, error)

    try:
        while not exhausted or admission or ready or active_downloads or active_processing:
            check_cancel(cancel)
            if auth_stop.is_set():
                exhausted = True
            while admission and (queued_bytes + admission[0][2] <= capacity or not ready):
                job, transferred, size = admission.popleft()
                ready.append((job, transferred, size))
                queued_bytes += size
                waiting(job, "等待后处理")
            while ready and len(active_processing) < process_limit:
                check_cancel(cancel)
                job, transferred, size = ready.popleft()
                queued_bytes -= size
                submit(processing, active_processing, job, process, transferred)
            while not exhausted and not auth_stop.is_set() and len(active_downloads) + len(admission) < download_limit:
                check_cancel(cancel)
                try:
                    job = next(source)
                except StopIteration:
                    exhausted = True
                    break
                if auth_stop.is_set():
                    exhausted = True
                    break
                check_cancel(cancel)
                started[id(job)] = job
                submit(downloads, active_downloads, job, download, job)
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
                value = future.result()
                if is_download and not isinstance(value, str):
                    size = downloaded_size(value)
                    if type(size) is not int or size <= 0:
                        raise ToolError("下载证据中的原始分片字节数必须为正整数")
            except Exception as error:
                settle(job, None, error)
            else:
                if is_download and not isinstance(value, str):
                    admission.append((job, value, size))
                    waiting(job, "等待队列容量")
                else:
                    settle(job, value, None)
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
                value = future.result()
            except Exception as task_error:
                outcomes[id(job)] = (None, task_error)
            except BaseException:
                continue
            else:
                if isinstance(value, str):
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
