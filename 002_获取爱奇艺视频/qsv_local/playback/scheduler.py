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


def schedule(jobs: list, config: dict, controls, update, *, discovery=None, admit=None, is_cancelled=None, retries=None) -> None:
    """调度 jobs 的单集阶段；config 限流，controls 取消，update 登记状态，无返回值。

    可选 discovery 为逐链接目录迭代器，在同一处理池推进；admit 在主线程接收目录批次或完成 None，
    返回新增的 (条目, 阶段生成器) 列表。is_cancelled 可判断单项取消；目录最多一个在途请求。
    retries 可接收当前是否空闲并返回待重试条目与生成器；可在空闲时等待网页操作，返回空列表后结束调度。
    有待解析任务且未发生队列背压时，处理池至少两个位置会为解析保留一个位置；总处理上限不变。
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

    def remove_cancelled(queue, sized=False):
        """移除 queue 中已取消项并登记状态；sized 表示队列记录包含字节数，返回释放字节。"""
        released = 0
        for _ in range(len(queue)):
            record = queue.popleft()
            item, steps = record[:2]
            if is_cancelled is not None and is_cancelled(item):
                steps.close()
                update(item, "cancelled", None)
                if sized:
                    released += record[2]
            else:
                queue.append(record)
        return released

    try:
        while True:
            check_cancel(controls.cancel)
            idle = not (pending or prepared or waiting or ready or active_downloads or active_processing or catalog_open)
            if retries is not None:
                pending.extend(retries(idle))
            if idle and not pending:
                break
            remove_cancelled(pending)
            remove_cancelled(prepared)
            remove_cancelled(waiting)
            queued -= remove_cancelled(ready, sized=True)
            while waiting and (queued + waiting[0][2] <= capacity or not ready):
                item, steps, size = waiting.popleft()
                ready.append((item, steps, size))
                queued += size
                update(item, "waiting_processing", None)
            while len(active_processing) < config["max_in_flight"]:
                preparing = sum(future.phase == "initial" for future in active_processing)
                can_prepare = not waiting and queued < capacity
                reserve = (can_prepare and config["max_in_flight"] > 1 and (pending or catalog_open))
                if (can_prepare and pending and len(prepared) + preparing < config["download"]["threads"]
                        and (not ready or config["max_in_flight"] > 1 and not preparing)):
                    item, steps = pending.popleft()
                    phase = "initial"
                elif (can_prepare and catalog_open and not discovering and not pending
                      and (not ready or config["max_in_flight"] > 1)):
                    future = processing.submit(next, source, None)
                    future.phase = "discovery"
                    active_processing[future] = (None, source)
                    discovering = True
                    continue
                elif ready:
                    # 已准备的任务有限且下载尚在进行时，为后续解析保留一个共享处理位置。
                    if reserve and not preparing and not discovering and len(active_processing) >= config["max_in_flight"] - 1:
                        break
                    item, steps, size = ready.popleft()
                    queued -= size
                    update(item, "processing", None)
                    phase = "processing"
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
                    if kind != "done" and is_cancelled is not None and is_cancelled(item):
                        steps.close()
                        update(item, "cancelled", None)
                        continue
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
                    update(item, "cancelled" if is_cancelled is not None and is_cancelled(item) else "failed", error)
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
