"""下载独立限流，解析与后处理共用处理池；队列容量按已下载字节背压。"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from .runtime import TaskCancelled, check_cancel
from .errors import safe_message

class PendingDownload(Exception):
    """本项因共享认证失败跳过；不发布成品。"""

class Scheduler:
    """供批次继承的阶段调度；子类提供任务步骤、日志、取消和资源配置。"""
    @staticmethod
    def _advance(steps):
        """推进单集生成器 steps 一次；返回阶段标记及字节数，结束时返回报告标记和报告。"""
        try:
            return next(steps)
        except StopIteration as done:
            return "done", done.value

    def run(self, tasks: list[dict]) -> dict:
        """登记 tasks，按独立下载上限及共享处理上限调度；预解析和媒体队列有界，返回终态计数。"""
        pending = iter([(task, self.journal.add(task)) for task in tasks])
        resolving, prepared, waiting, ready = deque(), deque(), deque(), deque()
        active_downloads, active_processing = {}, {}
        capacity = int(self.config["download"]["queue_capacity_gb"] * 1024**3)
        queued_bytes = 0
        summary = {key: 0 for key in ("completed", "reused", "failed", "interrupted", "skipped")}
        self.summary = summary
        exhausted = False
        downloads = ThreadPoolExecutor(max_workers=self.config["download"]["threads"], thread_name_prefix="youku-download")
        processing = ThreadPoolExecutor(max_workers=self.config["max_in_flight"], thread_name_prefix="youku-process")

        def finish(entry: dict, future) -> None:
            """处理 entry 的完成 future，记录终态或将生成器放入下一阶段，无返回值。"""
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
            while active_downloads or active_processing or waiting or ready or resolving or prepared or not exhausted:
                check_cancel(self.cancel)
                reason = self.dispatch_stop_reason()
                if reason and not exhausted:
                    exhausted = True
                    for task, entry in pending:
                        summary["skipped"] += 1
                        self.journal.update(entry, status="skipped", detail=reason)
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
                    elif not exhausted:
                        # 预解析至多缓存一组下载名额；等待媒体入队时停止扩大现场。
                        preparing = sum(future.phase == "initial" for future in active_processing)
                        if waiting or len(prepared) + preparing >= self.config["download"]["threads"]:
                            break
                        try:
                            task, entry = next(pending)
                        except StopIteration:
                            exhausted = True
                            break
                        steps = self._job_steps(task, entry)
                        phase = "initial"
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
                        entry = active_downloads.pop(future, None)
                        if entry is None:
                            entry = active_processing.pop(future)
                        finish(entry, future)
        except (KeyboardInterrupt, TaskCancelled):
            self.cancel.set()
            downloads.shutdown(wait=True, cancel_futures=True)
            processing.shutdown(wait=True, cancel_futures=True)
            for future, entry in list(active_downloads.items()) + list(active_processing.items()):
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
            raise
        finally:
            self.cancel.set()
            downloads.shutdown(wait=True, cancel_futures=True)
            processing.shutdown(wait=True, cancel_futures=True)
        return summary
