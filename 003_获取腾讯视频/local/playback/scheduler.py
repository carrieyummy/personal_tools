"""下载独立限流，解析与后处理共用处理池；队列容量按已下载字节背压。"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from ..runtime import TaskCancelled, check_cancel
from .transport import safe_message
from ..batch import PendingDownload

class UrlScheduler:
    """供批次继承的阶段调度；子类提供任务步骤、日志、取消和资源配置。"""
    @staticmethod
    def _advance(steps):
        """推进单集生成器 steps 一次；返回阶段标记及字节数，结束时返回报告标记和报告。"""
        try:
            return next(steps)
        except StopIteration as done:
            return "done", done.value

    def run(self, tasks: list[dict], discovery=None) -> dict:
        """调度已知 tasks 及可选 Discovery 来源 discovery，返回包含已登记总数的终态计数。

        目录每次仅展开一个清单项，与逐集解析、续签及后处理共享处理池；完整目录按剧交接，
        主循环不执行目录网络请求。任务元数据可继续补全，媒体预解析及下载后队列保持有界。
        """
        pending = deque()
        entries_by_identity = {}
        resolving, prepared, waiting, ready = deque(), deque(), deque(), deque()
        active_downloads, active_processing = {}, {}
        capacity = int(self.config["download"]["queue_capacity_gb"] * 1024**3)
        queued_bytes = 0
        summary = {key: 0 for key in ("completed", "reused", "failed", "interrupted", "skipped")}
        if discovery is not None:
            summary["total"] = 0
        self.summary = summary
        catalog_open = discovery is not None and discovery.position < len(discovery.items)
        discovery_active, discovery_closed = False, False
        stopped = None
        downloads = ThreadPoolExecutor(max_workers=self.config["download"]["threads"], thread_name_prefix="tencent-download")
        processing = ThreadPoolExecutor(max_workers=self.config["max_in_flight"], thread_name_prefix="tencent-process")

        def register(new_tasks: list[dict], failures: list[dict], progress: dict | None = None,
                     memberships: list | None = None) -> None:
            """批量登记 new_tasks/failures 和目录 progress，补充 memberships 分组；只将新任务入队。"""
            additions = [(entries_by_identity[identity], group) for identity, group in memberships or []]
            records = new_tasks + [{**failure, "status":"failed"} for failure in failures]
            entries = self.journal.add_many(records, progress, additions)
            if discovery is not None:
                summary["total"] += len(records)
            summary["failed"] += len(failures)
            for task, entry in zip(new_tasks, entries):
                pending.append((task, entry))
                if discovery is not None:
                    entries_by_identity[(task["series_id"], task["video_id"])] = entry

        def close_discovery(status: str, detail: str) -> None:
            """以 status 结束目录发现；detail 说明停止原因，未展开链接按清单项记录而不虚构集数。"""
            nonlocal discovery_closed, catalog_open
            catalog_open = False
            if discovery is None or discovery_closed:
                return
            remaining = discovery.items[discovery.position:]
            terminal = "skipped" if status == "stopped" else "interrupted"
            records = [{**discovery.failure(item, detail, index), "status":terminal}
                       for index, item in enumerate(remaining, discovery.position)]
            progress = discovery.progress(status)
            progress["remaining_requests"] = len(remaining)
            self.journal.add_many(records, progress)
            summary["total"] += len(records)
            summary[terminal] += len(records)
            discovery_closed = True

        def finish(entry: dict | None, future) -> None:
            """接收完成 future；entry 为单集日志，目录任务为 None，结果仅由调度线程登记及交接。"""
            nonlocal discovery_active, catalog_open, discovery_closed
            if future.phase == "discovery":
                discovery_active = False
                result = future.result()
                register(result["tasks"], result["failures"], result["progress"], result["memberships"])
                catalog_open = result["progress"]["status"] != "completed"
                discovery_closed = not catalog_open
                progress = result["progress"]
                print(f"目录展开：{progress['processed_requests']}/{progress['total_requests']} 个清单项，"
                      f"已发现 {progress['discovered_tasks']} 集" + ("，目录已完成" if not catalog_open else "，继续补全"), flush=True)
                return
            try:
                kind, value = future.result()
                if kind == "done":
                    summary[value["status"]] += 1
                    self.journal.update(entry, progress=100.0, detail=value.get("output", ""), **value)
                elif kind == "ready":
                    self.notify(entry, "waiting_queue", detail="下载完成，等待队列容量")
                    waiting.append((entry, future.steps, value))
                elif kind == "download":
                    self.notify(entry, "waiting_download", detail="来源已就绪，等待下载名额")
                    prepared.append((entry, future.steps))
                elif kind in ("retry", "resolve"):
                    self.notify(entry, "waiting_processing", detail="等待解析处理名额")
                    resolving.append((entry, future.steps))
                else:
                    raise RuntimeError("未知 URL 任务阶段")
            except Exception as error:
                status = "interrupted" if isinstance(error, TaskCancelled) else "skipped" if isinstance(error, PendingDownload) else "failed"
                summary[status] += 1
                self.journal.update(entry, status=status, detail=safe_message(error))

        def submit(executor, active: dict, entry: dict, steps, phase: str) -> None:
            """将 entry 的 steps 交给 executor，按 phase 记录预解析占用并登记到 active；无返回值。"""
            future = executor.submit(self._advance, steps)
            future.steps = steps
            future.phase = phase
            active[future] = entry

        try:
            register(tasks, [], discovery.progress() if discovery is not None else None)
            while active_downloads or active_processing or waiting or ready or resolving or prepared or pending or catalog_open:
                check_cancel(self.cancel)
                reason = self.dispatch_stop_reason()
                if reason:
                    stopped = reason
                    catalog_open = False
                    while pending:
                        task, entry = pending.popleft()
                        summary["skipped"] += 1
                        self.journal.update(entry, status="skipped", detail=reason)
                    if not discovery_active:
                        close_discovery("stopped", reason)
                while waiting and (queued_bytes + waiting[0][2] <= capacity or not ready):
                    entry, steps, size = waiting.popleft()
                    ready.append((entry, steps, size))
                    queued_bytes += size
                    self.notify(entry, "waiting_processing", detail="已下载，等待后处理")
                while len(active_processing) < self.config["max_in_flight"]:
                    # 已下载项优先释放磁盘积压；续签和来源重试也只在共享处理池执行。
                    if ready:
                        entry, steps, size = ready.popleft()
                        queued_bytes -= size
                        phase = "processing"
                    elif resolving:
                        entry, steps = resolving.popleft()
                        phase = "resolving"
                    else:
                        # 预解析至多缓存一组下载名额；等待媒体入队时停止扩大现场。
                        preparing = sum(future.phase == "initial" for future in active_processing)
                        if pending and not waiting and len(prepared) + preparing < self.config["download"]["threads"]:
                            task, entry = pending.popleft()
                            steps = self._job_steps(task, entry)
                            phase = "initial"
                        elif catalog_open and not discovery_active and not waiting and not stopped:
                            # 目录也占同一处理池的一个名额；不在主循环中 next() 等待网络。
                            future = processing.submit(discovery.advance)
                            future.phase, future.steps = "discovery", None
                            active_processing[future] = None
                            discovery_active = True
                            continue
                        else:
                            break
                    submit(processing, active_processing, entry, steps, phase)
                # 下载完成项不再占下载名额；媒体队列积压时暂停新下载以维持背压。
                while prepared and not waiting and len(active_downloads) < self.config["download"]["threads"]:
                    entry, steps = prepared.popleft()
                    submit(downloads, active_downloads, entry, steps, "download")
                futures = set(active_downloads) | set(active_processing)
                if futures:
                    done, _ = wait(futures, timeout=self.interval, return_when=FIRST_COMPLETED)
                    for future in done:
                        entry = active_downloads.pop(future) if future in active_downloads else active_processing.pop(future)
                        finish(entry, future)
            if stopped:
                close_discovery("stopped", stopped)
        except BaseException as error:
            self.cancel.set()
            downloads.shutdown(wait=True, cancel_futures=True)
            processing.shutdown(wait=True, cancel_futures=True)
            for future, entry in list(active_downloads.items()) + list(active_processing.items()):
                if future.phase == "discovery":
                    if not future.cancelled():
                        try:
                            finish(entry, future)
                        except TaskCancelled:
                            pass
                    continue
                if not future.cancelled():
                    finish(entry, future)
                else:
                    future.steps.close()
                    summary["interrupted"] += 1
                    self.journal.update(entry, status="interrupted", detail="批次中断")
            suspended = [(entry, steps) for entry, steps, _ in list(waiting) + list(ready)]
            for entry, steps in suspended + list(resolving) + list(prepared):
                try:
                    steps.close()
                except Exception:
                    pass
                summary["interrupted"] += 1
                self.journal.update(entry, status="interrupted", detail="批次中断")
            for task, entry in pending:
                summary["interrupted"] += 1
                self.journal.update(entry, status="interrupted", detail="中断前未派发")
            close_discovery("interrupted" if isinstance(error, (KeyboardInterrupt, TaskCancelled)) else "failed",
                            "目录尚未展开，批次已中断")
            if not isinstance(error, TaskCancelled):
                raise
        finally:
            self.cancel.set()
            downloads.shutdown(wait=True, cancel_futures=True)
            processing.shutdown(wait=True, cancel_futures=True)
        return summary
