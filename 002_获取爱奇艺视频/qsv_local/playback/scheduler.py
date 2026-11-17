"""独立限制媒体下载，解析和后处理共用处理池，已下载字节队列有界。"""

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from ..runtime import check_cancel


def advance(steps):
    """推进单个任务生成器 steps；返回阶段名称及字节数或最终结果。"""
    try:
        return next(steps)
    except StopIteration as result:
        return "done", result.value


def schedule(jobs: list, config: dict, controls, update, *, discovery=None, admit=None) -> None:
    """调度 jobs 的单集阶段；config 限流，controls 取消，update 登记状态，无返回值。

    可选 discovery 为逐链接目录迭代器，在同一处理池推进；admit 在主线程接收目录批次或完成 None，
    返回新增的 (条目, 阶段生成器) 列表。目录最多一个在途请求，不阻塞媒体派发及完成通知。
    """
    pending = deque(jobs)
    prepared, waiting, ready = deque(), deque(), deque()
    active_downloads, active_processing = {}, {}
    source = iter(discovery) if discovery is not None else None
    catalog_open, discovering = source is not None, False
    capacity = int(config["download"]["queue_capacity_gb"] * 1024**3)
    queued = 0
    downloads = ThreadPoolExecutor(max_workers=config["download"]["threads"], thread_name_prefix="iqiyi-download")
    processing = ThreadPoolExecutor(max_workers=config["max_in_flight"], thread_name_prefix="iqiyi-process")

    def submit(pool, active, item, steps, phase):
        """向 pool 提交 item 的 steps，记录 phase 并登记到 active；无返回值。"""
        future = pool.submit(advance, steps)
        future.phase = phase
        active[future] = (item, steps)

    try:
        while pending or prepared or waiting or ready or active_downloads or active_processing or catalog_open:
            check_cancel(controls.cancel)
            while waiting and (queued + waiting[0][2] <= capacity or not ready):
                item, steps, size = waiting.popleft()
                ready.append((item, steps, size))
                queued += size
                update(item, "waiting_processing", None)
            while len(active_processing) < config["max_in_flight"]:
                if ready:
                    item, steps, size = ready.popleft()
                    queued -= size
                    update(item, "processing", None)
                    phase = "processing"
                else:
                    preparing = sum(future.phase == "initial" for future in active_processing)
                    if pending and not waiting and len(prepared) + preparing < config["download"]["threads"]:
                        item, steps = pending.popleft()
                        phase = "initial"
                    elif catalog_open and not discovering and not waiting:
                        future = processing.submit(next, source, None)
                        future.phase = "discovery"
                        active_processing[future] = (None, source)
                        discovering = True
                        continue
                    else:
                        break
                submit(processing, active_processing, item, steps, phase)
            while prepared and not waiting and len(active_downloads) < config["download"]["threads"]:
                item, steps = prepared.popleft()
                submit(downloads, active_downloads, item, steps, "download")
            futures = set(active_downloads) | set(active_processing)
            if not futures:
                continue
            done, _ = wait(futures, timeout=controls.interval, return_when=FIRST_COMPLETED)
            for future in done:
                was_download = future in active_downloads
                item, steps = (active_downloads if was_download else active_processing).pop(future)
                if future.phase == "discovery":
                    discovering = False
                    batch = future.result()
                    pending.extend(admit(batch))
                    catalog_open = batch is not None
                    continue
                try:
                    kind, result = future.result()
                    if kind == "download":
                        prepared.append((item, steps))
                        update(item, "waiting_download", None)
                    elif kind == "ready":
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
        remaining = list(pending) + list(prepared) + [(item, steps) for item, steps, _ in waiting] + [(item, steps) for item, steps, _ in ready]
        for future, (item, steps) in list(active_downloads.items()) + list(active_processing.items()):
            if future.phase == "discovery":
                if not future.cancelled():
                    try:
                        batch = future.result()
                    except BaseException:
                        pass
                    else:
                        remaining.extend(admit(batch))
                continue
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
        if source is not None:
            close_source = getattr(source, "close", None)
            if close_source is not None:
                close_source()
