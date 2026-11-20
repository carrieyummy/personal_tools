"""读取已保存的下载快照，为运行中的批次提供只读进度页面。"""

from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit
import webbrowser

import yaml

from .configuration import resolve_path


def serve(config_path: Path) -> None:
    """按 config_path 主配置读取日志，在本机启动只读网页并阻塞至中断，无返回值。"""
    config_path = config_path.expanduser().resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8-sig"))
    output = resolve_path(config["output_path"], config_path.parent)
    state_path = output / config["logging"]["playback_state_file"]
    interval = config["logging"]["refresh_interval_seconds"]
    template = Path(__file__).with_name("templates").joinpath("playback_log.html").read_text(encoding="utf-8")

    def snapshot() -> dict:
        """读取最近一次保存的日志，返回标记为只读的快照；不修改原日志。"""
        with state_path.open("rb") as stream:
            data = json.load(stream)
            saved = datetime.fromtimestamp(Path(stream.name).stat().st_mtime).astimezone().isoformat(timespec="seconds")
        if not isinstance(data.get("runs"), list) or not data["runs"]:
            raise ValueError("下载日志没有批次记录")
        data.update(read_only=True, retry_enabled=False, closed=data["runs"][-1].get("status") != "running",
                    captured_at=saved)
        for run in data["runs"]:
            for entry in run["entries"]:
                entry.update(file_url="", report_url="")
        return data

    snapshot()

    class Handler(BaseHTTPRequestHandler):
        """仅提供本机只读下载快照及页面。"""

        def log_message(self, format: str, *args) -> None:
            """忽略 HTTP 访问日志；format 和 args 为服务端消息参数。"""
            return

        def do_GET(self) -> None:
            """读取原子保存的快照并响应页面或状态请求，文件读取失败返回 503。"""
            route = urlsplit(self.path).path
            if route == "/api/ack":
                body, content_type = b"{}", "application/json"
            elif route in ("/", "/api/state", "/export"):
                try:
                    data = snapshot()
                    if route == "/api/state":
                        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                        content_type = "application/json; charset=utf-8"
                    else:
                        payload = json.dumps(data, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
                        body = (template.replace("__DATA__", payload).replace("__LIVE__", json.dumps(route == "/"))
                                .replace("__INTERVAL__", str(interval * 1000))).encode("utf-8")
                        content_type = "text/html; charset=utf-8"
                except (OSError, ValueError, KeyError, TypeError):
                    self.send_error(503)
                    return
            else:
                self.send_error(404)
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.daemon_threads = True
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"只读进度页面：{url}，按 Ctrl+C 关闭页面服务。", flush=True)
        if config["logging"]["open_browser"]:
            webbrowser.open(url)
        server.serve_forever()
