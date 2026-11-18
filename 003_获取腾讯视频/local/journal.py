"""记录批次实时状态，提供仅监听本机的页面与不可覆盖的离线 HTML 报告。"""

from copy import deepcopy
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
from threading import Condition, RLock, Thread
import time
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

from .storage import atomic_json, timestamp


PAGE = Path(__file__).with_name("templates").joinpath("download_log.html").read_text(encoding="utf-8")


class Journal:
    """线程安全的当前批次记录；阶段变化即时保存，进度写盘按配置限频。"""

    def __init__(self, config: dict):
        """按 config 创建 JSON 历史、本地 HTTP 服务和批次；记录格式损坏时拒绝覆盖。"""
        self.config, self.lock = config, RLock()
        self.condition = Condition(self.lock)
        self.clients = {}
        self.receipts = set()
        self.closed = False
        self.started_monotonic = time.monotonic()
        self.path = config["output_path"] / config["logging"]["state_file"]
        self.history = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"version": 1, "runs": []}
        if not isinstance(self.history, dict) or self.history.get("version") != 1 or not isinstance(self.history.get("runs"), list):
            raise ValueError("批次日志格式无效")
        for run in self.history["runs"]:
            if not isinstance(run, dict) or not isinstance(run.get("entries"), list):
                raise ValueError("批次历史记录格式无效")
            for entry in run["entries"]:
                if not isinstance(entry, dict) or not isinstance(entry.get("video_id"), str):
                    raise ValueError("批次历史条目格式无效")
                entry.pop("_start", None)
                if entry.get("status") in ("running", "pending"):
                    entry.update(status="interrupted", detail="上次运行中断")
            if run.get("status") == "running":
                run.update(status="interrupted", finished=timestamp())
                discovery = run.get("discovery")
                if discovery and discovery.get("status") == "running":
                    discovery.update(status="interrupted", remaining_requests=(
                        discovery["total_requests"] - discovery["processed_requests"]))
        self.run = {"id": uuid4().hex, "started": timestamp(), "status": "running", "entries": []}
        self.history["runs"].append(self.run)
        self.last_write = 0.0
        self.server = ThreadingHTTPServer(("127.0.0.1", config["logging"]["port"]), self.handler())
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.save()
        self.url = f"http://127.0.0.1:{self.server.server_port}/"

    def handler(self):
        """创建回环页面、历史快照、终态确认和受限成品访问的处理器，返回处理器类。"""
        journal = self

        class Handler(BaseHTTPRequestHandler):
            """仅响应页面和 JSON 快照，不暴露本地文件。"""

            def log_message(self, format, *args):
                """忽略标准访问日志；format 和 args 为 HTTP 消息参数，无返回值。"""
                return

            def do_GET(self):
                """响应页面、快照、确认或已登记成品；拒绝外部 Host 和未知路径，无返回值。"""
                if self.headers.get("Host") != urlsplit(journal.url).netloc:
                    self.send_error(403)
                    return
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                client = query.get("client", [""])[0]
                attachment = None
                if parsed.path == "/":
                    client = uuid4().hex
                    with journal.condition:
                        journal.clients[client] = time.monotonic()
                    body, kind = journal.render(journal.snapshot(), True, client).encode("utf-8"), "text/html; charset=utf-8"
                elif parsed.path == "/state":
                    with journal.condition:
                        if client in journal.clients:
                            journal.clients[client] = time.monotonic()
                    body, kind = json.dumps(journal.snapshot(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
                elif parsed.path == "/ack":
                    with journal.condition:
                        if not journal.closed or client not in journal.clients or query.get("run", [""])[0] != journal.run["id"]:
                            self.send_error(409)
                            return
                    body, kind = b"{}", "application/json; charset=utf-8"
                elif parsed.path == "/snapshot":
                    body, kind = journal.render(journal.snapshot(), False).encode("utf-8"), "text/html; charset=utf-8"
                    attachment = f"{Path(journal.config['logging']['html_file']).stem}_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.html"
                elif re.fullmatch(r"/files/\d+/\d+", parsed.path):
                    run_index, entry_index = (int(item) for item in parsed.path.split("/")[2:])
                    with journal.lock:
                        try:
                            entry = journal.history["runs"][run_index]["entries"][entry_index]
                            target = Path(entry["output"]).resolve()
                            root = journal.config["output_path"].resolve()
                            allowed = entry.get("status") in ("completed", "reused") and target.is_relative_to(root) and target.is_file()
                        except (IndexError, KeyError, TypeError, OSError, ValueError):
                            allowed = False
                    if not allowed:
                        self.send_error(404)
                        return
                    try:
                        with target.open("rb") as stream:
                            self.send_response(200)
                            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                            self.send_header("Content-Length", str(target.stat().st_size))
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("X-Content-Type-Options", "nosniff")
                            self.end_headers()
                            while chunk := stream.read(1024 * 1024):
                                self.wfile.write(chunk)
                    except (OSError, ConnectionError):
                        pass
                    return
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if attachment:
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(attachment))
                self.end_headers()
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                    if parsed.path == "/ack":
                        with journal.condition:
                            journal.receipts.add(client)
                            journal.condition.notify_all()
                except (ConnectionError, OSError):
                    pass

        return Handler

    def snapshot(self) -> dict:
        """返回历史的展示白名单快照；隐藏内部状态和来源对象，避免向页面泄露敏感数据。"""
        with self.lock:
            runs = []
            for run_index, run in enumerate(self.history["runs"]):
                item = {key: deepcopy(run[key]) for key in ("id", "started", "finished", "status", "summary", "elapsed_seconds", "authentication") if key in run}
                item["entries"] = []
                for entry_index, entry in enumerate(run["entries"]):
                    row = {key: deepcopy(entry[key]) for key in ("video_id", "title", "groups", "page_url", "status", "stage", "detail", "progress", "stages", "resources", "device", "output", "authentication", "downloaded_bytes", "total_bytes", "bytes_per_second") if key in entry}
                    if row.get("status") in ("completed", "reused") and row.get("output"):
                        row["file_url"] = f"/files/{run_index}/{entry_index}"
                    item["entries"].append(row)
                runs.append(item)
            return {"runs": runs, "closed": self.closed, "current_id": self.run["id"], "captured_at": timestamp()}

    def render(self, data: dict, live: bool, client: str = "") -> str:
        """将白名单 data 渲染为自包含 HTML；live 启用轮询，client 是服务生成的页面标识。"""
        payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        return PAGE.replace("__LIVE__", "true" if live else "false").replace(
            "__INTERVAL__", str(int(self.config["logging"]["refresh_interval_seconds"] * 1000))).replace(
            "__CLIENT__", json.dumps(client)).replace("__DATA__", payload)

    def save(self) -> None:
        """按配置有界重试共享占用后原子保存历史；调用者持有锁，成功才更新写盘时间，无返回值。"""
        atomic_json(self.path, self.history, retry=self.config["logging"]["atomic_replace"])
        self.last_write = time.monotonic()

    def add(self, task: dict) -> dict:
        """为含 video_id 的任务 task 新增并保存进度条目；返回条目，批量目录交接使用 add_many。"""
        return self.add_many([task])[0]

    def add_many(self, tasks: list[dict], discovery: dict | None = None,
                 memberships: list[tuple[dict, dict]] | None = None) -> list[dict]:
        """一次保存 tasks 的新条目及目录 discovery 进度，返回新条目列表。

        tasks 含 video_id，可携带终态 status/detail；memberships 为已有条目与新增 URL 分组的配对，
        在同一日志锁内补充分组，不改任务状态，也不把工作线程中的目录对象共享到日志。
        """
        with self.lock:
            entries = []
            for task in tasks:
                entry = {"video_id": task["video_id"], "title": task.get("title", task["video_id"]),
                         "status": task.get("status", "pending"), "stage": "", "detail": task.get("detail", ""),
                         "progress": None, "stages": {}, "resources": {}}
                if "groups" in task:
                    entry["groups"] = deepcopy(task["groups"])
                if "page_url" in task:
                    entry["page_url"] = task["page_url"]
                self.run["entries"].append(entry)
                entries.append(entry)
            for entry, group in memberships or []:
                if group not in entry.setdefault("groups", []):
                    entry["groups"].append(deepcopy(group))
            if discovery is not None:
                self.run["discovery"] = deepcopy(discovery)
            self.save()
            return entries

    def update(self, entry: dict, stage: str | None = None, **fields) -> None:
        """线程安全更新 entry 的 stage 和 fields，记录阶段耗时；无返回值。"""
        with self.lock:
            now = time.monotonic()
            changed = stage is not None and stage != entry["stage"]
            if changed:
                previous = entry["stage"]
                if previous and entry.get("_start") is not None:
                    entry["stages"][previous] = round(now - entry["_start"], 3)
                entry["stage"], entry["progress"], entry["_start"] = stage, None, now
            entry.update(fields)
            if fields.get("status") in ("completed", "reused", "failed", "interrupted", "skipped"):
                start = entry.pop("_start", None)
                if start is not None and entry["stage"]:
                    entry["stages"][entry["stage"]] = round(now - start, 3)
            if changed or "status" in fields or now - self.last_write >= self.config["logging"]["progress_interval_seconds"]:
                self.save()
        if changed or "status" in fields:
            print(f"[{entry['title']}] {entry['stage']} {entry['status']} {entry['detail']}", flush=True)

    def record_resource(self, entry: dict, resource: str, wait_seconds: float, execution_seconds: float) -> None:
        """累计 entry 对 resource 的等待和持有秒数；耗时均为非负数，无返回值。"""
        with self.lock:
            timing = entry.setdefault("resources", {}).setdefault(resource, {"wait_seconds": 0.0, "execution_seconds": 0.0})
            timing["wait_seconds"] = round(timing["wait_seconds"] + wait_seconds, 3)
            timing["execution_seconds"] = round(timing["execution_seconds"] + execution_seconds, 3)

    def close(self, status: str, summary: dict) -> Path:
        """保存终态 status、summary，导出唯一离线报告并关闭服务；返回报告路径。"""
        try:
            with self.lock:
                for entry in self.run["entries"]:
                    start = entry.pop("_start", None)
                    if start is not None and entry["stage"]:
                        entry["stages"][entry["stage"]] = round(time.monotonic() - start, 3)
                self.run.update(status=status, summary=summary, finished=timestamp(),
                                elapsed_seconds=round(time.monotonic() - self.started_monotonic, 3))
                self.save()
                self.closed = True
                snapshot = self.snapshot()
            base = Path(self.config["logging"]["html_file"]).stem
            path = self.path.parent / f"{base}_{datetime.now():%Y%m%d_%H%M%S}_{self.run['id'][:8]}.html"
            with path.open("x", encoding="utf-8") as stream:
                stream.write(self.render(snapshot, False))
            with self.condition:
                timeout = self.config["logging"]["final_state_timeout_seconds"]
                cutoff = time.monotonic() - max(timeout, self.config["logging"]["refresh_interval_seconds"])
                active = {client for client, seen in self.clients.items() if seen >= cutoff}
                deadline = time.monotonic() + timeout
                while active - self.receipts:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        print("等待页面确认最终状态超时，请查看离线报告。", flush=True)
                        break
                    self.condition.wait(remaining)
            return path
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
