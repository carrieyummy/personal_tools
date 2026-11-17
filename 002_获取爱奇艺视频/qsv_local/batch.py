"""有界流水线批次调度：异步指纹、串行 SDK、独立封装及验证、串行发布和索引提交。"""

from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from pathlib import Path
import sys
from threading import Event

from .activity_log import Journal
from .discovery import Task, discover
from .download_state import DownloadNotReady, DownloadStatus, path_key, require_completed
from .migration import migrate
from .records import DirectoryIndex
from .runtime import TaskCancelled, check_cancel
from .storage import fingerprint, output_lock, publish, read_json, timestamp
from .verification import check_gpu
from .workflow import cleanup, error_status, finalize_verification, mux, prepare, save_report, verify


@dataclass(eq=False)
class Job:
    """单个在途视频；task/activity/work/started 为来源、日志、现场和开始时间；
    index/target/report 为目录索引、目标路径和阶段报告，stage/device 为阶段及验证设备，
    published/committed 区分文件已发布与索引已提交，用于失败和中断回收。
    """

    task: Task
    activity: dict
    work: Path
    started: str
    index: DirectoryIndex | None = None
    target: Path | None = None
    report: dict | None = None
    stage: str = "reading"
    device: str = ""
    published: bool = False
    committed: bool = False


class Pipeline:
    """主线程独占索引和状态提交；工作线程仅执行本任务阶段，FFmpeg 自身运行在独立进程。"""

    def __init__(self, config: dict, journal: Journal, tasks: list[Task], summary: dict):
        """使用已校验 config、运行日志 journal、发现任务 tasks 和统计容器 summary 建立有界流水线；不启动线程或读取源视频。"""
        self.config, self.journal, self.summary = config, journal, summary
        self.pending = deque(zip(tasks, [journal.add(task) for task in tasks]))
        self.cancel = Event()
        self.devices = [name for name in ("gpu", "cpu") if config["decode_verification"][name]]
        if not self.devices:
            raise ValueError("decode_verification.cpu/gpu 至少开启一个")
        # 等待阶段的任务也计入配置的在途上限。
        self.limit = config["max_in_flight"]
        self.jobs: list[Job] = []
        self.active: dict[Future, Job] = {}
        self.indexes: dict[Path, DirectoryIndex | Exception] = {}
        self.download_status = DownloadStatus(config)
        self.pools: dict[str, ThreadPoolExecutor] = {}
        with journal.lock:
            journal.run.update(decode_verification=deepcopy(config["decode_verification"]), in_flight_limit=self.limit)

    def notify(self, job: Job):
        """返回绑定 job 日志条目的回调，避免并发任务引用其他循环项；不在回调中改写目录索引。"""
        return partial(self.journal.update, job.activity)

    def admit(self) -> None:
        """从待处理队列接纳一个视频，并异步计算源指纹；索引由调度线程维护，无返回值。"""
        task, activity = self.pending.popleft()
        work = self.config["work_dir"] / self.journal.run["id"] / "videos" / task.relative
        job = Job(task, activity, work, timestamp())
        self.jobs.append(job)
        relative = task.relative.as_posix()
        print(f"[{self.summary['total'] - len(self.pending)}/{self.summary['total']}] {relative}", flush=True)
        try:
            check_cancel(self.cancel)
            reason = self.download_status.reason(task.source)
            if reason:
                raise DownloadNotReady(reason)
            if self.download_status.metadata.get(path_key(task.source)) != task.metadata:
                raise DownloadNotReady("客户端下载命名元数据发生变化")
            self.notify(job)("reading", detail="检查内容指纹及历史版本", work_dir=str(work))
            folder = task.target.parent
            if folder not in self.indexes:
                try:
                    self.indexes[folder] = DirectoryIndex(folder, self.config["output_path"])
                except Exception as error:
                    self.indexes[folder] = error
            index = self.indexes[folder]
            if isinstance(index, Exception):
                raise ValueError(f"目录索引无法加载：{index}")
            job.index = index
            job.stage = "fingerprinting"
            cached = deepcopy(index.data["sources"].get(relative))
            self.active[self.pools["fingerprinting"].submit(
                fingerprint, task.source, self.config["copy_buffer_bytes"], cached, cancel_event=self.cancel)] = job
        except BaseException as error:
            self.fail(job, error)
            if not isinstance(error, Exception):
                raise

    def complete_admission(self, job: Job, source_fp: dict) -> None:
        """在主线程按 job 和 source_fp 检查迁移、复用及目标命名；更新索引并排队准备，无返回值。"""
        task, index = job.task, job.index
        relative = task.relative.as_posix()
        check_cancel(self.cancel)
        reason = self.download_status.reason(task.source)
        if reason:
            raise DownloadNotReady(reason)
        if self.download_status.metadata.get(path_key(task.source)) != task.metadata:
            raise DownloadNotReady("客户端下载命名元数据发生变化")
        current = task.source.stat()
        if (current.st_size, current.st_mtime_ns) != (source_fp["size_bytes"], source_fp["mtime_ns"]):
            job.stage = "fingerprinting"
            self.active[self.pools["fingerprinting"].submit(
                fingerprint, task.source, self.config["copy_buffer_bytes"], source_fp, cancel_event=self.cancel)] = job
            return
        self.notify(job)("migrating", detail="检查旧版已验证成品")
        migrated = migrate(self.config, task, index, source_fp, self.journal.run["id"])
        if migrated:
            self.summary["migrated"] += 1
        self.notify(job)("reading", detail="比较源内容与已验证成品")
        reusable = index.reusable(relative, source_fp, self.config["copy_buffer_bytes"])
        if reusable:
            index.data["sources"][relative] = source_fp
            index.result(relative, self.journal.run["id"], "reused", file_id=reusable["id"], started_at=job.started,
                         work_dir=reusable.get("work_dir", ""))
            output = str(index.folder / reusable["path"])
            self.summary["reused"] += 1
            self.summary["results"].append({"source": relative, "status": "reused", "output": output})
            self.notify(job)(status="reused", output=output, work_dir=reusable.get("work_dir", ""),
                             detail="旧成品已迁移并复用" if migrated else "源内容及成品校验匹配，自动跳过")
            self.jobs.remove(job)
            return
        job.target = index.destination(relative, task.target)
        job.stage = "ready_prepare"

    def publish_job(self, job: Job) -> tuple[dict, dict]:
        """复制已验证 job 并返回成品和当前源指纹；仅工作线程调用，不更新索引；发布完成后标记以便中断回收。"""
        check_cancel(self.cancel)
        report = job.report
        require_completed(self.config, job.task.source, expected=job.task.metadata)
        current = fingerprint(job.task.source, self.config["copy_buffer_bytes"], report["source_fingerprint"],
                              cancel_event=self.cancel)
        if current["sha256"] != report["source_fingerprint"]["sha256"]:
            raise RuntimeError("发布前源文件发生变化")
        self.notify(job)("publishing", detail="校验复制成品并保存目录索引")
        require_completed(self.config, job.task.source, expected=job.task.metadata)
        output_fp = publish(Path(report["candidate"]), job.target, report["output_fingerprint"]["sha256"],
                            self.config["copy_buffer_bytes"], cancel_event=self.cancel)
        job.published = True
        return output_fp, current

    def commit(self, job: Job, result: tuple[dict, dict]) -> None:
        """在调度线程提交 job 的发布 result；只回滚本次提交开始时的最新索引，成功后按配置清理，无返回值。"""
        output_fp, current = result
        report, index = job.report, job.index
        relative = job.task.relative.as_posix()
        before = deepcopy(index.data)
        try:
            report.update(output=str(job.target), output_fingerprint=output_fp, work_dir=str(job.work))
            save_report(report, job.work)
            entry = index.add_file(relative, report["source_fingerprint"], job.target, output_fp, report, report["media"])
            index.data["sources"][relative] = current
            index.result(relative, self.journal.run["id"], "completed", file_id=entry["id"],
                         work_dir=str(job.work), started_at=job.started)
        except BaseException:
            index.data = before
            raise
        job.committed = True
        self.summary["completed"] += 1
        self.summary["results"].append({"source": relative, "status": "completed", "output": str(job.target)})
        self.notify(job)(status="completed", output=str(job.target), detail="转换、完整解码及成品校验通过")
        self.jobs.remove(job)
        if not self.config["keep_intermediates"]:
            try:
                cleanup(job.work)
            except OSError as error:
                print(f"成品已保存，中间文件清理失败：{error}", file=sys.stderr, flush=True)

    def fail(self, job: Job, error: BaseException) -> None:
        """在调度线程登记 job 的 error 并保留现场；从最新磁盘索引恢复，删除未登记的本次成品，无返回值。"""
        if job.committed:
            raise error
        status = "skipped" if isinstance(error, DownloadNotReady) else error_status(error)
        message = str(error) or "用户中断"
        index = job.index
        if index is not None:
            try:
                index.data = DirectoryIndex(index.folder, self.config["output_path"]).data
            except Exception:
                pass
        if job.published and index is not None:
            registered = any(item["path"] == job.target.name for item in index.data["files"])
            if not registered:
                job.target.unlink(missing_ok=True)
        if index is not None and status != "skipped":
            try:
                index.result(job.task.relative.as_posix(), self.journal.run["id"], status, error=message,
                             work_dir=str(job.work), started_at=job.started)
            except Exception as record_error:
                message += f"；结果记录保存失败：{record_error}"
        if job.work.is_dir():
            try:
                report = job.report or read_json(job.work / "report.json")
            except (OSError, ValueError):
                report = {"source": str(job.task.source), "work_dir": str(job.work)}
            report.update(status=error_status(error), error=message)
            save_report(report, job.work, error)
        self.notify(job)(status=status, detail=message)
        relative = job.task.relative.as_posix()
        if status == "skipped":
            self.summary["pending_downloads"] += 1
            with self.journal.lock:
                self.journal.run["pending_downloads"].append({"source": relative, "reason": message})
        elif status == "failed":
            self.summary["failed"] += 1
            print(f"转换失败：{relative}：{message}", file=sys.stderr, flush=True)
        self.summary["results"].append({"source": relative, "status": status, "error": message})
        self.jobs.remove(job)

    def collect(self) -> None:
        """接收已完成阶段，在主线程推进状态或提交；失败隔离，中断异常继续向上传播，无返回值。"""
        for future, job in list(self.active.items()):
            if not future.done():
                continue
            del self.active[future]
            try:
                result = future.result()
                if job.stage == "fingerprinting":
                    self.complete_admission(job, result)
                elif job.stage == "publishing":
                    self.commit(job, result)
                else:
                    job.report = result
                    if job.stage == "preparing":
                        if result.get("status") != "extracted":
                            raise RuntimeError("准备阶段未提供提取证明")
                        job.stage = "extracted"
                    elif job.stage == "muxing":
                        if result.get("status") != "prepared":
                            raise RuntimeError("封装阶段未提供候选媒体")
                        job.stage = "prepared"
                        self.notify(job)("waiting_verification", detail="候选已封装，等待空闲验证设备")
                    elif job.stage == "verifying":
                        if result.get("status") != "decoded":
                            raise RuntimeError("解码阶段未提供完整解码证明")
                        job.stage = "decoded"
                    elif job.stage == "checking":
                        if result.get("status") != "verified" or result.get("source_unchanged") is not True:
                            raise RuntimeError("转换未提供完整验证证明")
                        job.stage = "verified"
            except BaseException as error:
                self.fail(job, error)
                if not isinstance(error, Exception):
                    raise

    def dispatch(self) -> None:
        """将就绪任务分配到各独占阶段及 GPU/CPU 槽；每个槽最多一个任务，无返回值。"""
        active_jobs = list(self.active.values())
        for waiting, running, function in (("ready_prepare", "preparing", prepare),
                                            ("extracted", "muxing", mux),
                                            ("decoded", "checking", finalize_verification)):
            if any(job.stage == running for job in active_jobs):
                continue
            ready = next((job for job in self.jobs if job.stage == waiting), None)
            if ready is not None:
                ready.stage = running
                args = ((self.config, ready.task.source, ready.work, self.notify(ready), self.cancel)
                        if running == "preparing" else
                        (self.config, ready.report, ready.work, self.notify(ready), self.cancel))
                self.active[self.pools[running].submit(function, *args)] = ready
        if not any(job.stage == "publishing" for job in active_jobs):
            ready = next((job for job in self.jobs if job.stage == "verified"), None)
            if ready is not None:
                ready.stage = "publishing"
                self.active[self.pools["publishing"].submit(self.publish_job, ready)] = ready
        for device in self.devices:
            if any(job.stage == "verifying" and job.device == device for job in active_jobs):
                continue
            ready = next((job for job in self.jobs if job.stage == "prepared"), None)
            if ready is not None:
                ready.stage, ready.device = "verifying", device
                future = self.pools[device].submit(verify, self.config, ready.report, ready.work,
                                                    self.notify(ready), device, self.cancel)
                self.active[future] = ready

    def run(self) -> None:
        """启动独立阶段线程并驱动有界调度；异常时协作取消、等待线程退出，再登记未完成项，无返回值。"""
        try:
            if self.pending and "gpu" in self.devices:
                check_gpu(self.config, self.config["work_dir"] / self.journal.run["id"], self.cancel)
            self.pools = {name: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"qsv-{name}")
                          for name in ["fingerprinting", "preparing", "muxing", "checking", "publishing", *self.devices]}
            while self.pending or self.jobs:
                self.collect()
                self.dispatch()
                if (self.pending and len(self.jobs) < self.limit
                        and not any(job.stage == "fingerprinting" for job in self.active.values())):
                    self.admit()
                if self.active:
                    wait(self.active, timeout=self.config["runtime"]["cancel_poll_interval_seconds"],
                         return_when=FIRST_COMPLETED)
        except BaseException:
            self.cancel.set()
            for future in self.active:
                future.cancel()
            for pool in self.pools.values():
                pool.shutdown(wait=True, cancel_futures=True)
            for job in self.jobs[:]:
                self.fail(job, TaskCancelled("批次已中断，保留未提交现场"))
            raise
        finally:
            for pool in self.pools.values():
                pool.shutdown(wait=True, cancel_futures=True)


def run_batch(config: dict) -> dict:
    """按 config 执行有界批次流水线；返回完成/复用/失败/迁移统计及逐项结果，中断保存现场后抛出 KeyboardInterrupt。"""
    with output_lock(config["output_path"]):
        pending = []
        tasks = discover(config, pending=pending)
        summary = {"total": len(tasks), "completed": 0, "reused": 0, "failed": 0, "migrated": 0,
                   "pending_downloads": len(pending), "results": []}
        with Journal(config) as journal:
            print(f"实时日志：{journal.url}", flush=True)
            with journal.lock:
                journal.run["pending_downloads"] = pending
            if pending:
                print(f"忽略 {len(pending)} 个下载未完成或状态不明的文件，未读取其内容。", flush=True)
            if not tasks:
                print("没有匹配的 QSV 文件。", flush=True)
            Pipeline(config, journal, tasks, summary).run()
            order = {task.relative.as_posix(): number for number, task in enumerate(tasks)}
            summary["results"].sort(key=lambda result: order[result["source"]])
            with journal.lock:
                journal.run["summary"] = {key: value for key, value in summary.items() if key != "results"}
        summary["report"] = str(journal.snapshot_path) if journal.snapshot_path else None
        return summary
