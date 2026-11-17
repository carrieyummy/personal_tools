"""独立调度链接下载、已下载字节队列和有界后处理。"""

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def advance(steps):
    """推进单个任务生成器 steps；返回阶段名称及字节数或最终结果。"""
    try:
        return "ready", next(steps)
    except StopIteration as result:
        return "done", result.value


def schedule(jobs: list, config: dict, controls, update) -> None:
    """调度 jobs 中的 (条目, 生成器)，config 限制下载、队列和后处理；controls 控制取消，update 登记状态；无返回值。"""
    pending = deque(jobs)
    waiting, ready = deque(), deque()
    active_downloads, active_processing = {}, {}
    capacity = int(config["download"]["queue_capacity_gb"] * 1024**3)
    queued = 0
    downloads = ThreadPoolExecutor(max_workers=config["download"]["threads"], thread_name_prefix="iqiyi-download")
    processing = ThreadPoolExecutor(max_workers=config["max_in_flight"], thread_name_prefix="iqiyi-process")

    def submit(pool, active, item, steps):
        """向 pool 提交 item 的 steps 并登记到 active；无返回值。"""
        active[pool.submit(advance, steps)] = (item, steps)

    try:
        while pending or waiting or ready or active_downloads or active_processing:
            while waiting and (queued + waiting[0][2] <= capacity or not ready):
                item, steps, size = waiting.popleft()
                ready.append((item, steps, size))
                queued += size
                update(item, "waiting_processing", None)
            while ready and len(active_processing) < config["max_in_flight"]:
                item, steps, size = ready.popleft()
                queued -= size
                update(item, "processing", None)
                submit(processing, active_processing, item, steps)
            while pending and len(active_downloads) + len(waiting) < config["download"]["threads"]:
                item, steps = pending.popleft()
                submit(downloads, active_downloads, item, steps)
            futures = set(active_downloads) | set(active_processing)
            if not futures:
                continue
            done, _ = wait(futures, timeout=controls.interval, return_when=FIRST_COMPLETED)
            for future in done:
                was_download = future in active_downloads
                item, steps = (active_downloads if was_download else active_processing).pop(future)
                try:
                    kind, result = future.result()
                    if kind == "ready" and was_download:
                        if type(result) is not int or result <= 0:
                            raise ValueError("下载完成的分片字节数必须为正整数")
                        waiting.append((item, steps, result))
                        update(item, "waiting_queue", None)
                    elif kind == "done":
                        update(item, "done", result)
                    else:
                        raise RuntimeError("链接任务产生未知阶段")
                except Exception as error:
                    steps.close()
                    update(item, "failed", error)
    except BaseException:
        controls.cancel.set()
        for future in list(active_downloads) + list(active_processing):
            future.cancel()
        downloads.shutdown(wait=True, cancel_futures=True)
        processing.shutdown(wait=True, cancel_futures=True)
        remaining = list(pending) + [(item, steps) for item, steps, _ in waiting] + [(item, steps) for item, steps, _ in ready]
        for future, (item, steps) in list(active_downloads.items()) + list(active_processing.items()):
            if not future.cancelled():
                try:
                    kind, value = future.result()
                    if kind == "done":
                        update(item, "done", value)
                        continue
                except BaseException:
                    pass
            remaining.append((item, steps))
        for item, steps in remaining:
            try:
                steps.close()
            finally:
                update(item, "interrupted", None)
        raise
    finally:
        downloads.shutdown(wait=True, cancel_futures=True)
        processing.shutdown(wait=True, cancel_futures=True)
