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
    手动重试在各等待队列优先派发，已执行项不被抢占；普通下载预解析已满时，最多额外准备一个重试项。
    有待解析任务且未发生队列背压时，处理池至少两个位置会为解析保留一个位置；总处理上限不变。
    """
    pending = deque(jobs)
    prepared, waiting, ready = deque(), deque(), deque()
    active_downloads, active_processing = {}, {}
    retry_steps = set()
    source = iter(discovery) if discovery is not None else None
    catalog_open, discovering = source is not None, False
    capacity = int(config["download"]["queue_capacity_gb"] * 1024**3)
    download_limit, processing_limit = config["download"]["threads"], config["max_in_flight"]
    queued = 0
    downloads = ThreadPoolExecutor(max_workers=download_limit, thread_name_prefix="iqiyi-download")
    processing = ThreadPoolExecutor(max_workers=processing_limit, thread_name_prefix="iqiyi-process")

    def enqueue(queue, record):
        """将 record 加入 queue；重试排在普通项之前，同类项按进入该队列的顺序排列，无返回值。"""
        if record[1] in retry_steps:
            for position, existing in enumerate(queue):
                if existing[1] not in retry_steps:
                    queue.insert(position, record)
                    return
        queue.append(record)

    def has_retry(queue) -> bool:
        """返回当前按优先级排列的 queue 是否含有待重试项。"""
        return bool(queue and queue[0][1] in retry_steps)

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
                retry_steps.discard(steps)
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
                for record in retries(idle):
                    retry_steps.add(record[1])
                    enqueue(pending, record)
            if idle and not pending:
                break
            remove_cancelled(pending)
            remove_cancelled(prepared)
            remove_cancelled(waiting)
            queued -= remove_cancelled(ready, sized=True)
            while waiting and (queued + waiting[0][2] <= capacity or not ready):
                item, steps, size = waiting.popleft()
                enqueue(ready, (item, steps, size))
                queued += size
                update(item, "waiting_processing", None)
            while len(active_processing) < processing_limit:
                preparing = [steps for future, (_, steps) in active_processing.items() if future.phase == "initial"]
                retry_pending, retry_ready = has_retry(pending), has_retry(ready)
                # 普通预解析队列满时，也允许准备一个重试，仍使用同一个处理池。
                extra = retry_pending and not has_retry(prepared) and not any(steps in retry_steps for steps in preparing)
                prepare_room = len(prepared) + len(preparing) < download_limit + int(extra)
                can_prepare = not waiting and queued < capacity
                reserve = can_prepare and processing_limit > 1 and (pending or catalog_open)
                prepare_first = not ready or (processing_limit > 1 and not preparing)
                if retry_pending != retry_ready:
                    prepare_first = retry_pending
                if can_prepare and pending and prepare_room and prepare_first:
                    item, steps = pending.popleft()
                    phase = "initial"
                elif (can_prepare and catalog_open and not discovering and not pending and not retry_ready
                      and (not ready or processing_limit > 1)):
                    future = processing.submit(next, source, None)
                    future.phase = "discovery"
                    active_processing[future] = (None, source)
                    discovering = True
                    continue
                elif ready:
                    # 已准备的任务有限且下载尚在进行时，为后续解析保留一个共享处理位置。
                    if (reserve and (not retry_ready or retry_pending) and not preparing and not discovering
                            and len(active_processing) >= processing_limit - 1):
                        break
                    item, steps, size = ready.popleft()
                    queued -= size
                    update(item, "processing", None)
                    phase = "processing"
                else:
                    break
                submit(processing, active_processing, item, steps, phase)
            retry_preparing = any(future.phase == "initial" and steps in retry_steps
                                  for future, (_, steps) in active_processing.items())
            while prepared and not waiting and len(active_downloads) < download_limit:
                # 给正在等来源的重试留一个下载位置，其他空位仍可派发已准备的普通项。
                if (retry_preparing or has_retry(pending)) and not has_retry(prepared) and len(active_downloads) == download_limit - 1:
                    break
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
                        retry_steps.discard(steps)
                        update(item, "cancelled", None)
                        continue
                    if kind == "download":
                        enqueue(prepared, (item, steps))
                        update(item, "waiting_download", None)
                    elif kind == "ready":
                        if type(result) is not int or result <= 0:
                            raise ValueError("下载完成的分片字节数必须为正整数")
                        enqueue(waiting, (item, steps, result))
                        update(item, "waiting_queue", None)
                    elif kind == "done":
                        retry_steps.discard(steps)
                        update(item, "done", result)
                    else:
                        raise RuntimeError("链接任务产生未知阶段")
                except Exception as error:
                    steps.close()
                    retry_steps.discard(steps)
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
