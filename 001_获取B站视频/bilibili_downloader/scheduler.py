"""提供有界、按输入顺序补位的共享线程池，准备与结果处理始终在调用线程执行。"""

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Empty, SimpleQueue
from threading import Event
from typing import Callable, TypeVar

from .errors import check_cancel
from . import runtime


Job = TypeVar('Job')
Payload = TypeVar('Payload')
Result = TypeVar('Result')


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
