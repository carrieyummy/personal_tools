"""记录批次实时状态，提供仅监听本机的页面与不可覆盖的离线 HTML 报告。"""

from copy import deepcopy
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import RLock, Thread
import time
from uuid import uuid4

from .storage import atomic_json, timestamp


PAGE = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>腾讯视频转换</title>
<style>body{font:15px sans-serif;margin:2em;max-width:1000px}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #ddd;text-align:left;padding:.6em}pre{white-space:pre-wrap}</style>
<h1>腾讯视频转换</h1><p id="info"></p><table><thead><tr><th>剧集</th><th>状态</th><th>阶段</th><th>进度</th><th>详情</th></tr></thead><tbody id="rows"></tbody></table>
<script>const live=__LIVE__,interval=__INTERVAL__;let data=__DATA__;
function render(d){document.getElementById('info').textContent='批次：'+d.id+'　状态：'+d.status+'　开始：'+d.started;
const body=document.getElementById('rows');body.replaceChildren();for(const e of d.entries){let tr=document.createElement('tr');
for(const v of [e.title||e.video_id,e.status,e.stage,e.progress==null?'':e.progress.toFixed(1)+'%',e.detail||'']){let td=document.createElement('td');td.textContent=v||'';tr.appendChild(td)}body.appendChild(tr)}}
render(data);if(live)setInterval(()=>fetch('/state').then(r=>r.json()).then(render).catch(()=>{}),interval);</script></html>"""


class Journal:
    """线程安全的当前批次记录；阶段变化即时保存，进度写盘按配置限频。"""

    def __init__(self, config: dict):
        """按 config 创建 JSON 历史、本地 HTTP 服务和批次；记录格式损坏时拒绝覆盖。"""
        self.config, self.lock = config, RLock()
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
        """创建只提供本批次页面和状态的 HTTP 处理器，返回处理器类。"""
        journal = self

        class Handler(BaseHTTPRequestHandler):
            """仅响应页面和 JSON 快照，不暴露本地文件。"""

            def log_message(self, format, *args):
                """忽略标准访问日志；format 和 args 为 HTTP 消息参数，无返回值。"""
                return

            def do_GET(self):
                """响应页面或状态请求；其他路径返回 404，无返回值。"""
                with journal.lock:
                    data = deepcopy(journal.run)
                if self.path == "/":
                    body, kind = journal.render(data, True).encode("utf-8"), "text/html; charset=utf-8"
                elif self.path == "/state":
                    body, kind = json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def render(self, data: dict, live: bool) -> str:
        """将 data 转为安全自包含 HTML；live 控制是否定时刷新，返回页面字符串。"""
        payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        return PAGE.replace("__DATA__", payload).replace("__LIVE__", "true" if live else "false").replace(
            "__INTERVAL__", str(int(self.config["logging"]["refresh_interval_seconds"] * 1000)))

    def save(self) -> None:
        """将当前历史原子保存并更新写盘时间，无返回值；调用者持有锁。"""
        atomic_json(self.path, self.history)
        self.last_write = time.monotonic()

    def add(self, task: dict) -> dict:
        """为任务 task 新增进度条目，返回该条目；task 必须包含 video_id 和 title。"""
        with self.lock:
            entry = {"video_id": task["video_id"], "title": task.get("title", task["video_id"]),
                     "status": "pending", "stage": "", "detail": "", "progress": None, "stages": {}, "resources": {}}
            self.run["entries"].append(entry)
            self.save()
            return entry

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
                snapshot = deepcopy(self.run)
            base = Path(self.config["logging"]["html_file"]).stem
            path = self.path.parent / f"{base}_{datetime.now():%Y%m%d_%H%M%S}_{self.run['id'][:8]}.html"
            with path.open("x", encoding="utf-8") as stream:
                stream.write(self.render(snapshot, False))
            return path
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
