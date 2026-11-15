"""将活动状态提供给本地网页，独立保存 JSON 历史与不可变 HTML 快照。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from threading import RLock
import time
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from .config import Settings
from .errors import ToolError
from .log_server import LogServer
from .storage import record_io_lock, validate_path


STATUS_LABELS = {
    'pending': '等待下载', 'preparing': '准备中', 'downloading': '下载中',
    'processing': '封装及字幕处理', 'retry': '等待重选画质', 'queued': '等待合并',
    'merging': '合并中', 'completed': '已完成', 'reused': '已完成（复用）',
    'failed': '失败', 'interrupted': '已中断', 'skipped': '已跳过', 'warning': '完成但有提示',
    'running': '运行中',
}
TERMINAL = {'completed', 'reused', 'failed', 'interrupted', 'skipped', 'warning'}


def timestamp() -> str:
    """返回带本地时区的当前 ISO 时间；无参数。"""
    return datetime.now().astimezone().isoformat(timespec='seconds')


class Activity:
    """一条任务、分 P 或拼接活动的句柄，状态更新通过 journal 串行保存。"""

    def __init__(self, journal: HtmlLog, entry: dict[str, Any]) -> None:
        """保存 journal 日志和 entry 条目引用；无返回值。"""
        self.journal, self.entry = journal, entry
        self.last_progress = 0.0
        self.transfer_started: float | None = None
        self.previous_bytes = 0
        self.transferred_bytes = 0
        self.transfer_seconds = 0.0
        self.finished_transfer_seconds = 0.0
        self.transfer_intervals: list[list[float]] = []
        self.merge_started: float | None = None
        self.merge_duration: float | None = None

    def update(self, status: str | None = None, **changes: Any) -> None:
        """更新 status 及 changes 展示字段；记录实际开始及结束时间，关闭或终态后的迟到进度忽略，无返回值。"""
        with self.journal.lock:
            if self.journal.closed or (self.entry['status'] in TERMINAL and status is not None and status not in TERMINAL):
                return
            if status is not None:
                if status not in TERMINAL | {'pending', 'queued'} and not self.entry['active_started']:
                    self.entry['active_started'] = timestamp()
                self.entry['status'] = status
                if status in TERMINAL:
                    self.entry['finished'] = timestamp()
                    changes.setdefault('eta_seconds', 0 if status in {'completed', 'reused'} else None)
                    if status in {'completed', 'reused'}:
                        changes.setdefault('progress', 100.0)
            self.entry.update(changes, updated=timestamp())
            self.journal.write()

    def start_transfer(self, label: str, resumed_bytes: int = 0) -> None:
        """开始 label 原始流的计量；resumed_bytes 为现有断点字节数，不计入本次平均速度；无返回值。"""
        with self.journal.lock:
            self.transfer_started = time.monotonic()
            self.transfer_intervals.append([time.time(), time.time()])
            self.previous_bytes = resumed_bytes
            self.last_progress = float('-inf')
            self.update('downloading', progress=None, progress_label=label, current_bytes=resumed_bytes,
                        total_bytes=None, eta_seconds=None)

    def end_transfer(self) -> None:
        """结算当前流的有效传输时长，排除流间等待及后续封装字幕；无参数、无返回值。"""
        with self.journal.lock:
            if self.transfer_started is None:
                return
            self.transfer_seconds = self.finished_transfer_seconds + max(0.0, time.monotonic() - self.transfer_started)
            self.transfer_intervals[-1][1] = self.transfer_intervals[-1][0] + self.transfer_seconds - self.finished_transfer_seconds
            self.finished_transfer_seconds = self.transfer_seconds
            self.transfer_started = None
            self.update(processed_bytes=self.transferred_bytes, metric_seconds=self.transfer_seconds,
                        metric_intervals=deepcopy(self.transfer_intervals),
                        average_speed_bps=self.transferred_bytes / self.transfer_seconds if self.transfer_seconds > 0 else None)

    def progress(self, status: dict[str, Any]) -> None:
        """采样 yt-dlp status 的当前流进度；累计本次传输字节及有效时长，按配置节流落盘，结束样本立即保存；无返回值。"""
        with self.journal.lock:
            if self.journal.closed or self.entry['status'] in TERMINAL:
                return
            now = time.monotonic()
            downloaded = status.get('downloaded_bytes') or 0
            # 服务端不支持续传或重试从零开始时，重新计入实际传输的数据，而非扣减已传输量。
            self.transferred_bytes += max(0, downloaded - self.previous_bytes) if downloaded >= self.previous_bytes else downloaded
            self.previous_bytes = downloaded
            if self.transfer_started is None:
                self.transfer_started = now - max(0.0, status.get('elapsed') or 0.0)
                self.transfer_intervals.append([time.time() - max(0.0, status.get('elapsed') or 0.0), time.time()])
            self.transfer_seconds = self.finished_transfer_seconds + max(0.0, now - self.transfer_started)
            self.transfer_intervals[-1][1] = self.transfer_intervals[-1][0] + self.transfer_seconds - self.finished_transfer_seconds
            if status.get('status') != 'finished' and now - self.last_progress < self.journal.settings.log_progress_interval:
                return
            self.last_progress = now
            total = status.get('total_bytes') or status.get('total_bytes_estimate')
            percent = min(100.0, downloaded * 100 / total) if total else None
            detail = f'当前流：{downloaded / 1_000_000:.2f} MB'
            if total:
                detail += f' / {total / 1_000_000:.2f} MB'
            self.update('downloading', progress=percent, detail=detail, current_bytes=downloaded,
                        total_bytes=total, processed_bytes=self.transferred_bytes, metric_seconds=self.transfer_seconds,
                        metric_intervals=deepcopy(self.transfer_intervals),
                        average_speed_bps=self.transferred_bytes / self.transfer_seconds if self.transfer_seconds > 0 else None,
                        eta_seconds=0 if status.get('status') == 'finished' else status.get('eta'))

    def start_merge(self, duration: float | None, total_bytes: int) -> None:
        """开始合并计量；duration 为来源总秒数（未知用 None），total_bytes 为含重复分 P 的源字节总量；无返回值。"""
        with self.journal.lock:
            self.merge_started, self.merge_duration = time.monotonic(), duration
            self.last_progress = float('-inf')
            self.update('merging', progress=None, progress_label='合并媒体时间', total_bytes=total_bytes,
                        current_bytes=0, processed_bytes=0, metric_seconds=0.0, average_speed_bps=None, eta_seconds=None)

    def merge_progress(self, values: dict[str, str]) -> None:
        """采集 FFmpeg values 进度块；按媒体时间计算百分比、输出字节计算 MB/s，未知量保留为空；无返回值。"""
        with self.journal.lock:
            if self.journal.closed or self.entry['status'] in TERMINAL or self.merge_started is None:
                return
            now = time.monotonic()
            if values.get('progress') != 'end' and now - self.last_progress < self.journal.settings.log_progress_interval:
                return
            self.last_progress = now
            elapsed = max(0.0, now - self.merge_started)
            media_time = progress_number(values.get('out_time_us'))
            media_time = media_time / 1_000_000 if media_time is not None else None
            written = progress_number(values.get('total_size'))
            percent = min(99.9, media_time * 100 / self.merge_duration) if media_time is not None and self.merge_duration else None
            eta = max(0.0, (self.merge_duration - media_time) * elapsed / media_time) if self.merge_duration and media_time and elapsed else None
            self.update('merging', progress=percent, current_bytes=written, processed_bytes=written,
                        metric_seconds=elapsed, average_speed_bps=written / elapsed if written is not None and elapsed > 0 else None,
                        eta_seconds=eta, detail='正在无损拼接；进度按已处理媒体时间计算，平均速度为输出吞吐量')


def progress_number(value: Any) -> float | None:
    """将进度值 value 转为非负有限数字；FFmpeg 的 N/A、缺失值或非法值返回 None。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


class HtmlLog:
    """一次运行的日志所有者；网页只读内存，持久化失败不向业务传播，离线快照不覆盖已有文件。"""

    def __init__(self, settings: Settings, label: str) -> None:
        """按 settings 加载历史并启动 label 模式日志；历史损坏时报错以保留原始数据，无返回值。"""
        self.settings, self.lock = settings, RLock()
        self.path = settings.output_dir / settings.log_state_filename
        self.legacy_path = settings.output_dir / settings.log_filename
        for path in (self.path, self.legacy_path):
            validate_path(path, settings.output_dir)
        self.template = Path(__file__).with_name('templates').joinpath('download_log.html').read_text(encoding='utf-8')
        self.closed = False
        self.snapshot_path: Path | None = None
        self.data = self.load_history()
        self.data.update(storage_error='', revision=0, closed=False)
        self.run: dict[str, Any] = {'id': uuid4().hex, 'label': label, 'started': timestamp(), 'finished': '',
                                    'status': 'running', 'entries': [], 'detail': ''}
        self.data['runs'].append(self.run)
        self.write()
        self.server = LogServer(self)
        self.url = self.server.url

    def load_history(self) -> dict[str, Any]:
        """优先读 JSON，否则只读导入旧 HTML；修复旧运行的遗留活动状态，返回历史对象，无参数。"""
        data: dict[str, Any] = {'version': 1, 'runs': []}
        source = self.path if self.path.exists() else self.legacy_path
        if not source.exists():
            return data
        try:
            text = source.read_text(encoding='utf-8')
            if source == self.legacy_path:
                match = re.search(r'<script id="download-log-data" type="application/json">(.*?)</script>', text, re.S)
                if match is None:
                    raise ValueError('缺少结构化日志数据')
                text = match[1]
            data = json.loads(text)
            if data['version'] != 1 or not isinstance(data['runs'], list):
                raise ValueError('日志版本或运行列表无效')
            for run in data['runs']:
                run.setdefault('id', uuid4().hex)
                if run['status'] == 'running':
                    run.update(status='interrupted', finished=timestamp())
                for entry in run['entries']:
                    entry.setdefault('id', uuid4().hex)
                    entry.setdefault('active_started', entry.get('started', '') if entry['status'] != 'pending' else '')
                    if entry['status'] not in STATUS_LABELS:
                        raise ValueError('未知活动状态')
                    if entry['status'] not in TERMINAL:
                        entry.update(status='interrupted', detail='上次运行已结束，清理遗留的活动状态',
                                     updated=timestamp(), finished=entry.get('finished') or run.get('finished') or timestamp())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ToolError(f'无法读取日志历史 {source}：{exc}') from exc
        return data

    def __enter__(self) -> HtmlLog:
        """返回当前日志供 with 使用；无参数。"""
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> None:
        """按 exc_type/exc/traceback 保存终态、快照并关闭 HTTP 服务；日志写盘错误不覆盖业务异常，无返回值。"""
        with self.lock:
            if self.closed:
                return
            status = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed' if exc else 'completed'
            for entry in self.run['entries']:
                if entry['status'] not in TERMINAL:
                    ending = 'skipped' if entry['status'] == 'pending' else status if exc else 'skipped'
                    entry.update(status=ending, detail=str(exc) or ('取消时尚未开始' if ending == 'skipped' else '运行已结束，断点已保留'),
                                 updated=timestamp(), finished=timestamp())
            if not exc and any(entry['status'] in {'failed', 'warning', 'skipped'} for entry in self.run['entries']):
                status = 'warning'
            self.run.update(status=status, finished=timestamp(), detail=str(exc) if exc else '')
            self.closed = True
            self.data['closed'] = True
            self.write()
        try:
            self.export_snapshot()
        finally:
            self.server.close()
        if self.snapshot_path:
            print(f'离线日志快照：{self.snapshot_path}', flush=True)

    def add(self, kind: str, title: str, *, task: str = '', part: str = '',
            status: str = 'pending', detail: str = '') -> Activity:
        """新增 kind/title 活动；task 标识任务，part 为分 P/分组，status/detail 为初态；返回句柄，关闭后拒绝新增。"""
        with self.lock:
            if self.closed:
                raise RuntimeError('日志已关闭，不能新增活动')
            entry = {'id': uuid4().hex, 'kind': kind, 'title': title, 'task': task, 'part': part, 'status': status,
                     'detail': detail, 'path': '', 'quality': '', 'progress': None,
                     'active_started': timestamp() if status not in TERMINAL | {'pending', 'queued'} else '',
                     'progress_label': '', 'current_bytes': None, 'total_bytes': None, 'processed_bytes': 0,
                     'metric_seconds': 0.0, 'average_speed_bps': None, 'eta_seconds': None,
                     'started': timestamp(), 'updated': timestamp(), 'finished': ''}
            self.run['entries'].append(entry)
            self.write()
            return Activity(self, entry)

    def merge_activity(self, title: str, *, task: str, part: str) -> Activity:
        """领取 title/task/part 对应的计划合并；未预建时新增，返回唯一活动句柄，避免计划与执行重复计数。"""
        with self.lock:
            for entry in self.run['entries']:
                if entry['kind'] == '合并' and entry['task'] == task and entry['part'] == part and entry['status'] in {'pending', 'queued'}:
                    return Activity(self, entry)
            return self.add('合并', title, task=task, part=part, status='merging')

    def snapshot(self, *, live: bool) -> dict[str, Any]:
        """返回独立的内存快照；live 决定媒体链接为 HTTP 路径或离线 file 路径，浏览器请求不读日志磁盘。"""
        with self.lock:
            data = deepcopy(self.data)
            data['captured_at'] = timestamp()
        root = self.settings.output_dir.resolve()
        for run in data['runs']:
            for entry in run['entries']:
                entry['file_url'] = ''
                if entry.get('path') and entry['status'] in ('completed', 'reused', 'warning'):
                    target = Path(entry['path']).resolve()
                    if target.is_relative_to(root):
                        entry['file_url'] = '/files/' + quote(target.relative_to(root).as_posix()) if live else target.as_uri()
        return data

    def render_document(self, *, live: bool) -> str:
        """从内存渲染页面；live 为 True 时轮询服务，False 时内嵌数据且不联网；返回 HTML。"""
        payload = json.dumps(self.snapshot(live=live), ensure_ascii=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
        return (self.template.replace('<!--DATA-->', payload)
                .replace('__LIVE__', json.dumps(live)).replace('__LABELS__', json.dumps(STATUS_LABELS, ensure_ascii=False))
                .replace('__INTERVAL_MS__', str(self.settings.log_refresh_interval * 1000)))

    def snapshot_filename(self) -> str:
        """返回带唯一运行和导出标识的 HTML 文件名；无参数，每次调用均不同。"""
        return f'{Path(self.settings.log_filename).stem}_{self.run["id"]}_{uuid4().hex[:8]}.html'

    def export_snapshot(self) -> None:
        """独占创建最终离线 HTML，不替换已打开文件；失败提示但不改变业务结局，无参数及返回值。"""
        path = self.settings.output_dir / self.snapshot_filename()
        try:
            with path.open('x', encoding='utf-8') as stream:
                stream.write(self.render_document(live=False))
            self.snapshot_path = path
        except OSError as exc:
            print(f'离线日志快照保存失败（不影响媒体结果）：{exc}', file=sys.stderr, flush=True)

    def write(self) -> bool:
        """在调用方持锁时原子保存 JSON，文件占用有限重试；失败保留内存状态且不抛 I/O 异常，返回是否成功。"""
        self.data['revision'] += 1
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {**self.data, 'storage_error': ''}
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix=f'.{self.path.stem}-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False)
            for attempt in range(self.settings.log_write_retries + 1):
                try:
                    with record_io_lock:
                        temporary.replace(self.path)
                    break
                except OSError as exc:
                    if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == self.settings.log_write_retries:
                        raise
                    time.sleep(self.settings.log_write_retry_interval)
            if self.data['storage_error']:
                print('日志持久化已恢复，最新状态已保存。', file=sys.stderr, flush=True)
            self.data['storage_error'] = ''
            return True
        except OSError as exc:
            if not self.data['storage_error']:
                print(f'日志暂时无法保存，下载及合并继续；后续将重试：{exc}', file=sys.stderr, flush=True)
            self.data['storage_error'] = str(exc)
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
