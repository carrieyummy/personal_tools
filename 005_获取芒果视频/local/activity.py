"""带历史记录的脱敏下载日志、仅监听回环地址的实时页面和独立离线报告。"""

from __future__ import annotations

from datetime import datetime
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Lock, Thread
import time
from typing import Any
from uuid import uuid4

from .storage import atomic_json, read_json


def page(state: dict[str, Any], interval: float, live: bool) -> str:
    """渲染安全 HTML；state 为日志快照，interval 为刷新间隔，live 控制轮询；返回完整页面。"""
    payload = json.dumps(state, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    refresh = f"setInterval(async()=>{{try{{render(await(await fetch('/state.json',{{cache:'no-store'}})).json())}}catch(e){{}}}},{interval * 1000});" if live else ""
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>芒果下载记录</title>
<style>body{font:15px system-ui;background:#10151d;color:#e5edf7;margin:30px}table{border-collapse:collapse;width:100%;margin:16px 0 32px}td,th{border-bottom:1px solid #344150;padding:10px;text-align:left;overflow-wrap:anywhere}small{color:#aab8cb}h2{color:#ffae65}</style>
<h1>芒果下载记录</h1><main id="root"></main><script>
function node(tag,text){let e=document.createElement(tag);e.textContent=text;return e}
function render(state){let root=document.getElementById('root');root.replaceChildren();for(let run of [...state.runs].reverse()){
root.append(node('h2',run.started+' · '+run.status));if(run.discovery){let d=run.discovery;root.append(node('p',({running:'目录展开中，任务总数待确定',completed:'目录展开完成',stopped:'目录展开已停止',interrupted:'目录展开已中断'}[d.status]||d.status)+' · 清单 '+d.processed_requests+'/'+d.total_requests+' · 已发现 '+d.discovered_tasks+' 集'))}let table=document.createElement('table');let head=document.createElement('tr');
for(let name of ['视频','状态','进度','说明'])head.append(node('th',name));table.append(head);
for(let item of Object.values(run.items)){let row=document.createElement('tr');for(let value of [item.title,item.stage,item.progress||'',item.message||''])row.append(node('td',value));table.append(row)}root.append(table)}}
render(''' + payload + ");" + refresh + "</script></html>"


class Activity:
    """单写入锁保护运行历史；config 控制根路径、节流和 HTTP；不在日志保存认证或网络地址。"""

    def __init__(self, config: dict[str, Any]):
        """读取 config 中日志与历史，创建本次运行；无返回值，尚未启动服务器。"""
        self.config, self.settings = config, config["logging"]
        self.path = config["output_path"] / self.settings["state_file"]
        self.state = read_json(self.path, {"schema": 1, "runs": []})
        if self.state.get("schema") != 1 or not isinstance(self.state.get("runs"), list):
            from .errors import ToolError
            raise ToolError("历史日志格式无效")
        for previous in self.state["runs"]:
            discovery = previous.get("discovery")
            if discovery and discovery.get("status") == "running":
                discovery["status"] = "interrupted"
        self.run = {"id": uuid4().hex, "started": datetime.now().isoformat(timespec="seconds"), "status": "运行中", "items": {}}
        self.state["runs"].append(self.run)
        self.lock, self.sampled, self.server = Lock(), {}, None

    def update(self, key: str, title: str, stage: str, progress: str = "", message: str = "") -> None:
        """记录阶段；key/title 为任务编号和标题，stage 为阶段，progress/message 为脱敏显示内容；无返回值。"""
        with self.lock:
            now = time.monotonic()
            previous = self.run["items"].get(key, {})
            if previous.get("stage") == stage and now - self.sampled.get(key, float("-inf")) < self.settings["progress_interval_seconds"]:
                return
            self.run["items"][key] = {"title": title, "stage": stage, "progress": progress, "message": message, "updated": datetime.now().isoformat(timespec="seconds")}
            self.sampled[key] = now
            atomic_json(self.path, self.state, self.settings)
            if previous.get("stage") != stage:
                print(f"[{stage}] {title}" + (f"：{message}" if message else ""), flush=True)

    def snapshot(self) -> dict[str, Any]:
        """取得隔离的日志快照；无参数；返回可安全交给 HTTP 线程的数据。"""
        with self.lock:
            return json.loads(json.dumps(self.state))

    def catalog(self, progress: dict[str, Any]) -> None:
        """批量保存目录 progress 的状态、清单完成数和已发现集数；不估计尚未查询的集数，无返回值。"""
        with self.lock:
            self.run["discovery"] = dict(progress)
            atomic_json(self.path, self.state, self.settings)

    def start(self) -> str:
        """启动本机实时页面；无参数；返回 127.0.0.1 URL，仅支持只读页面和日志快照。"""
        activity = self

        class Handler(BaseHTTPRequestHandler):
            """只读回环 HTTP 处理器；由标准库传入请求上下文。"""

            def do_GET(self):
                """返回页面或 JSON；无额外参数；未知路径返回 404，不映射文件系统。"""
                if self.path not in ("/", "/state.json"):
                    self.send_error(404)
                    return
                state = activity.snapshot()
                content = json.dumps(state, ensure_ascii=False) if self.path == "/state.json" else page(state, activity.settings["refresh_interval_seconds"], True)
                raw = content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8" if self.path == "/state.json" else "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format, *args):
                """禁用 HTTP 访问日志；format/args 为标准库信息；无返回值。"""

        self.server = ThreadingHTTPServer(("127.0.0.1", self.settings["port"]), Handler)
        self.server.daemon_threads = True
        Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}/"

    def finish(self, status: str) -> Path:
        """关闭运行；status 为批次结果；写历史与不覆盖的离线 HTML，停止 HTTP，返回报告路径。"""
        try:
            with self.lock:
                self.run.update(status=status, finished=datetime.now().isoformat(timespec="seconds"))
                self.run["counts"] = dict(Counter(item["stage"] for item in self.run["items"].values()))
                atomic_json(self.path, self.state, self.settings)
                base = self.config["output_path"] / self.settings["html_file"]
                stamp = datetime.now().strftime(self.config["naming"]["timestamp_format"])
                report = base.with_stem(base.stem + "_" + stamp + "_" + self.run["id"][:8])
                with report.open("x", encoding="utf-8") as output:
                    output.write(page(self.state, self.settings["refresh_interval_seconds"], False))
            return report
        finally:
            if self.server:
                self.server.shutdown()
                self.server.server_close()
