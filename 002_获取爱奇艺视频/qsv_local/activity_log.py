"""提供历次运行日志、本机实时页面和不可覆盖的离线报告。"""

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Event, RLock, Thread
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import uuid4

from .storage import atomic_json, read_json, time_tag, timestamp

TERMINAL = {"completed", "reused", "failed", "interrupted", "skipped", "cancelled"}
VERIFY_STAGES = {"waiting_verification", "verifying", "checking", "publishing"}


class Journal:
    """管理一次批次及历史日志；HTTP 只读取内存，网页查看不占用 JSON 文件。"""

    def __init__(self, config: dict, *, mode: str = "qsv"):
        """按 config 加载历史并启动服务；mode 为 qsv 或 playback，决定阶段、日志文件和模板，返回初始化实例。"""
        if mode not in ("qsv", "playback"):
            raise ValueError("未知的日志模式")
        self.mode = mode
        self.phases = ("download", "processing") if mode == "playback" else ("conversion", "verification")
        self.config, self.lock = config, RLock()
        self.root = config["output_path"]
        self.path = self.root / config["logging"]["playback_state_file" if mode == "playback" else "state_file"]
        self.closed, self.storage_error = False, ""
        self.cancellations, self.retry_request, self.stop_request = None, None, None
        self.control_token = uuid4().hex
        self.final_ack, self.has_client = Event(), False
        self.snapshot_path = None
        self.last_write = float("-inf")
        self.write_event, self.writer_stop = Event(), Event()
        self.writer = None
        self.template = Path(__file__).with_name("templates").joinpath("playback_log.html" if mode == "playback" else "conversion_log.html").read_text(encoding="utf-8")
        self.data = read_json(self.path) if self.path.exists() else {"version": 1, "runs": []}
        if self.data.get("version") != 1 or not isinstance(self.data.get("runs"), list):
            raise ValueError(f"运行日志格式无效，保留原数据：{self.path}")
        for run in self.data["runs"]:
            if (not isinstance(run, dict) or not isinstance(run.get("entries"), list)
                    or not isinstance(run.get("id"), str)):
                raise ValueError(f"运行历史格式无效：{self.path}")
            run.pop("awaiting_stop", None)
            for entry in run["entries"]:
                if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                        or not all(isinstance(entry.get(phase), dict) for phase in self.phases)):
                    raise ValueError(f"运行历史条目无效：{self.path}")
                if entry.get("status") not in TERMINAL:
                    entry.update(status="interrupted", detail="上次运行已中断", finished=timestamp())
                    for phase in self.phases:
                        if entry[phase].get("status") not in TERMINAL:
                            entry[phase]["status"] = "interrupted"
            if run.get("status") == "running":
                run.update(status="interrupted", finished=timestamp())
                if run.get("catalog_status") in ("resolving", "discovering"):
                    run["catalog_status"] = "interrupted"
        self.run = {"id": uuid4().hex, "started": timestamp(), "finished": "", "status": "running",
                    "entries": [], "summary": {}, "mode": mode, "catalog_status": "resolving" if mode == "playback" else "ready"}
        self.data.setdefault("revision", 0)
        self.server = ThreadingHTTPServer(("127.0.0.1", config["logging"]["port"]), self._handler())
        self.server.daemon_threads = True
        self.data["runs"].append(self.run)
        self.write()
        self.writer = Thread(target=self._write_loop, daemon=True, name="qsv-log-writer")
        self.writer.start()
        self.thread = Thread(target=self.server.serve_forever,
                             kwargs={"poll_interval": config["runtime"]["cancel_poll_interval_seconds"]},
                             daemon=True, name="qsv-log")
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        """创建绑定本日志内存的 HTTP 处理器类型；无参数，返回处理器类。"""
        journal = self

        class Handler(BaseHTTPRequestHandler):
            """仅服务本机页面、内存状态以及根目录边界内的成品和工作日志。"""

            def log_message(self, format: str, *args) -> None:
                """抑制默认访问日志；format/args 为 HTTP 内部消息，无返回值。"""
                return

            def do_POST(self) -> None:
                """处理本机页面的取消、单集重试或停止服务请求；校验来源、令牌和请求大小。"""
                if self.path not in ("/api/cancel", "/api/retry", "/api/stop") or journal.mode != "playback":
                    self.send_error(404)
                    return
                origin = f"http://127.0.0.1:{journal.server.server_port}"
                if (self.headers.get("Origin") != origin or self.headers.get("Host") != origin.removeprefix("http://")
                        or self.headers.get("X-Control-Token") != journal.control_token
                        or self.headers.get("Content-Type") != "application/json"):
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 4096:
                        raise ValueError("invalid size")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict) or not isinstance(payload.get("kind"), str) or not isinstance(payload.get("id"), str):
                        raise ValueError("invalid payload")
                except (ValueError, UnicodeDecodeError):
                    self.send_error(400)
                    return
                with journal.lock:
                    accepted = False
                    if not journal.closed:
                        if self.path == "/api/cancel" and journal.cancellations is not None:
                            accepted = journal.cancellations.cancel(payload["kind"], payload["id"])
                        elif self.path == "/api/retry" and payload["kind"] == "episode" and journal.retry_request is not None:
                            accepted = journal.retry_request(payload["id"])
                        elif self.path == "/api/stop" and payload["kind"] == "batch" and journal.stop_request is not None:
                            accepted = journal.stop_request(payload["id"])
                    if not accepted:
                        self.send_error(409)
                        return
                    journal.write()
                self._send(b"{}", "application/json")

            def do_GET(self) -> None:
                """处理当前 GET 请求；路径越界返回 404，页面或快照数据不从日志磁盘读取。"""
                route = unquote(urlsplit(self.path).path)
                try:
                    if route == "/":
                        journal.has_client = True
                        self._send(journal.render(live=True).encode("utf-8"), "text/html; charset=utf-8")
                    elif route == "/api/state":
                        journal.has_client = True
                        self._send(json.dumps(journal.snapshot(live=True), ensure_ascii=False).encode("utf-8"),
                                   "application/json; charset=utf-8")
                    elif route == "/api/ack":
                        revision = parse_qs(urlsplit(self.path).query).get("revision")
                        with journal.lock:
                            valid = journal.closed and revision == [str(journal.data["revision"])]
                        if not valid:
                            self.send_error(409)
                        else:
                            self._send(b"{}", "application/json")
                            journal.final_ack.set()
                    elif route == "/export":
                        path = journal.export()
                        if path is None:
                            self.send_error(503, "Unable to save report")
                        else:
                            self._send(path.read_bytes(), "text/html; charset=utf-8", path.name)
                    elif route.startswith(("/files/", "/logs/")):
                        is_media = route.startswith("/files/")
                        root = journal.root if is_media else journal.config["work_dir"]
                        relative = route[len("/files/") if is_media else len("/logs/"):]
                        target = (root / relative).resolve()
                        if not target.is_relative_to(root) or not target.is_file():
                            self.send_error(404)
                            return
                        self.send_response(200)
                        self.send_header("Content-Type", "video/mp4" if target.suffix.lower() == ".mp4" else "text/plain; charset=utf-8")
                        self.send_header("Content-Length", str(target.stat().st_size))
                        self.end_headers()
                        with target.open("rb") as stream:
                            while chunk := stream.read(journal.config["copy_buffer_bytes"]):
                                self.wfile.write(chunk)
                    else:
                        self.send_error(404)
                except (BrokenPipeError, ConnectionResetError):
                    return
                except OSError:
                    self.send_error(503)

            def _send(self, body: bytes, content_type: str, filename: str | None = None) -> None:
                """发送 body 字节及 content_type；filename 非空时设置下载附件名，无返回值。"""
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                if filename:
                    self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def __enter__(self):
        """进入日志生命周期；返回当前日志对象。"""
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        """根据异常 exc_type/exc/traceback 保存终态、导出快照并关闭服务，无返回值。"""
        try:
            with self.lock:
                status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed" if exc else "completed"
                for entry in self.run["entries"]:
                    if entry["status"] not in TERMINAL:
                        entry.update(status="interrupted" if exc else "skipped", finished=timestamp())
                        for phase in self.phases:
                            if entry[phase]["status"] not in TERMINAL:
                                entry[phase]["status"] = entry["status"]
                if not exc and (any(entry["status"] == "failed" for entry in self.run["entries"]) or self.run.get("catalog_errors")):
                    status = "warning"
                self.run.update(status=status, finished=timestamp())
                self.closed = True
                self.write()
            self.writer_stop.set()
            self.write_event.set()
            self.writer.join()
            self.export()
            if self.mode == "playback" and self.has_client:
                if not self.final_ack.wait(self.config["logging"]["final_state_timeout_seconds"]):
                    print("网页终态确认超时；完整结果已保存到离线报告。", file=sys.stderr, flush=True)
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
        if self.snapshot_path:
            print(f"离线报告：{self.snapshot_path}", flush=True)

    def add(self, task) -> dict:
        """为 task 创建待处理视频项；返回内存条目，来源路径只用于显示，不包含授权内容。"""
        with self.lock:
            entry = {"id": uuid4().hex, "source_relative": task.relative.as_posix(),
                     "directory": task.relative.parent.as_posix(), "title": task.source.name,
                     "status": "pending", "stage": "", "detail": "", "progress": None,
                     "started": "", "finished": "", "output": "", "work_dir": "",
                     "conversion": {"status": "pending"}, "verification": {"status": "pending"}}
            self.run["entries"].append(entry)
            self.write()
            return entry

    def add_episode(self, video: dict, quality: int, audio_quality: int) -> dict:
        """新增 video 单集进度及可选页面链接；quality/audio_quality 为实际配置排名，返回线程安全更新所需条目。"""
        with self.lock:
            if self.mode != "playback" or self.closed:
                raise RuntimeError("当前日志不能新增链接剧集")
            entry = {"id": uuid4().hex, "directory": video["album_id"], "series_title": video["title"],
                     "release_date": video.get("release_date"),
                     "episode": video["episode"], "tvid": video["tvid"], "title": video["episode_title"],
                     "play_url": video.get("play_url"),
                     "source_relative": f"{video['title']}/第{video['episode']}集", "status": "pending", "stage": "",
                     "detail": "", "progress": None, "started": "", "finished": "", "output": "", "work_dir": "",
                     "quality_rank": quality, "audio_quality_rank": audio_quality,
                     "download": {"status": "pending"}, "processing": {"status": "pending"}}
            self.run["entries"].append(entry)
            self.write()
            return entry

    def update(self, entry: dict, stage: str | None = None, *, status: str | None = None,
               progress: float | None = None, detail: str | None = None, device: str | None = None, **fields) -> None:
        """更新 entry 的 stage/status、progress、detail 及 fields；device 可记录实际 cpu/gpu；状态立即保存，进度按配置节流。"""
        with self.lock:
            if self.closed or entry["status"] in TERMINAL:
                return
            changed_stage = stage is not None and stage != entry["stage"]
            if not entry["started"]:
                entry["started"] = timestamp()
            if changed_stage:
                entry.update(stage=stage, progress=None)
            entry.update(fields)
            if device is not None:
                entry["processing" if self.mode == "playback" else "verification"]["device"] = device
            if detail is not None:
                entry["detail"] = detail
            if progress is not None:
                entry["progress"] = max(0.0, min(100.0, progress))
            if status:
                entry["status"] = status
            elif stage:
                entry["status"] = "running"
            if self.mode == "playback":
                phase = "download" if entry["stage"] in {"", "pending", "resetting_download", "waiting_source", "resolving_source", "authorizing", "checking_reuse", "validating_manifest", "downloading"} else "processing"
            else:
                phase = "verification" if entry["stage"] in VERIFY_STAGES else "conversion"
            entry[phase].update(status=entry["status"], stage=entry["stage"], progress=entry["progress"])
            if self.mode == "playback":
                entry[phase].update({key: value for key, value in fields.items() if key in {"current_bytes", "total_bytes", "speed_bps", "eta_seconds"}})
            if phase == self.phases[1]:
                entry[self.phases[0]].update(status="completed", progress=100.0)
            if entry["status"] in TERMINAL:
                entry["finished"] = timestamp()
                if status in {"completed", "reused"}:
                    for name in self.phases:
                        entry[name].update(status=status, progress=100.0)
                    entry["progress"] = 100.0
                elif phase == self.phases[0]:
                    entry[self.phases[1]]["status"] = "skipped"
            now = time.monotonic()
            if changed_stage or status or now - self.last_write >= self.config["logging"]["progress_interval_seconds"]:
                self.write()

    def reset_episode(self, entry: dict) -> None:
        """在日志锁内将已排队重试的失败条目 entry 重置为待处理，保留剧集和选档资料；无返回值。"""
        with self.lock:
            entry.update(status="pending", stage="", detail="已排队重新下载", progress=None, started="", finished="",
                         output="", work_dir="", download={"status": "pending"}, processing={"status": "pending"})
            for field in ("error", "retry_queued", "media_label", "quality_label", "report_file"):
                entry.pop(field, None)

    def write(self) -> None:
        """请求保存当前内存历史；启动时同步保存，运行时由写盘线程合并请求，无参数或返回值。"""
        with self.lock:
            if self.cancellations is not None:
                links, albums = self.cancellations.snapshot()
                self.run.update(links=links, cancelled_albums=albums)
            self.last_write = time.monotonic()
            self.data["revision"] += 1
            if self.writer is None:
                self._persist(deepcopy(self.data))
            else:
                self.write_event.set()

    def _write_loop(self) -> None:
        """合并写盘请求并保存独立快照；关闭时写入最终快照后退出，无参数或返回值。"""
        while True:
            self.write_event.wait()
            with self.lock:
                self.write_event.clear()
                snapshot = deepcopy(self.data)
                stopping = self.writer_stop.is_set()
            self._persist(snapshot)
            if stopping:
                return

    def _persist(self, snapshot: dict) -> None:
        """按配置重试保存独立 snapshot；失败时记录错误并保留内存状态，无返回值。"""
        logging = self.config["logging"]
        for attempt in range(logging["write_retries"] + 1):
            try:
                atomic_json(self.path, snapshot)
                with self.lock:
                    if self.storage_error:
                        print("运行日志持久化已恢复。", file=sys.stderr, flush=True)
                    self.storage_error = ""
                return
            except OSError as error:
                if attempt < logging["write_retries"]:
                    time.sleep(logging["write_retry_interval_seconds"])
                    continue
                with self.lock:
                    if not self.storage_error:
                        print(f"运行日志暂时无法保存，转换继续：{error}", file=sys.stderr, flush=True)
                    self.storage_error = str(error)

    def snapshot(self, *, live: bool) -> dict:
        """返回当前内存数据的独立快照；live 控制成品与报告使用 HTTP 链接还是离线文件链接。"""
        with self.lock:
            data = deepcopy(self.data)
            data.update(closed=self.closed, storage_error=self.storage_error, captured_at=timestamp())
            if self.cancellations is not None:
                links, albums = self.cancellations.snapshot()
                data["runs"][-1].update(links=links, cancelled_albums=albums)
            if live and self.mode == "playback":
                data["control_token"] = self.control_token
                data["retry_enabled"] = not self.closed and self.retry_request is not None
        for run in data["runs"]:
            for entry in run["entries"]:
                entry.update(file_url="", report_url="")
                for field, url_field, root, prefix in (
                    ("output", "file_url", self.root, "/files/"),
                    ("work_dir", "report_url", self.config["work_dir"], "/logs/"),
                ):
                    if not entry.get(field):
                        continue
                    target = Path(entry[field])
                    if field == "work_dir":
                        target /= entry.get("report_file", "full_report.json" if self.mode == "playback" else "report.json")
                    target = target.resolve()
                    if target.is_relative_to(root):
                        entry[url_field] = prefix + quote(target.relative_to(root).as_posix()) if live else target.as_uri()
        return data

    def render(self, *, live: bool) -> str:
        """渲染 live 指定的实时或离线 HTML；返回安全嵌入 JSON 的自包含页面。"""
        payload = json.dumps(self.snapshot(live=live), ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        return (self.template.replace("__LIVE__", json.dumps(live))
                .replace("__INTERVAL__", str(self.config["logging"]["refresh_interval_seconds"] * 1000))
                .replace("__DATA__", payload))

    def export(self) -> Path | None:
        """独占创建带时间戳的离线报告；重名追加序号，返回新路径；失败提示但不影响已生成媒体。"""
        with self.lock:
            base = Path(self.config["logging"]["playback_html_file" if self.mode == "playback" else "html_file"]).stem + "_" + time_tag()
            number = 0
            while True:
                suffix = "" if number == 0 else f"_{number:02d}"
                path = self.root / f"{base}{suffix}.html"
                try:
                    self.root.mkdir(parents=True, exist_ok=True)
                    with path.open("x", encoding="utf-8") as stream:
                        stream.write(self.render(live=False))
                    self.snapshot_path = path
                    return path
                except FileExistsError:
                    number += 1
                except OSError as error:
                    print(f"离线报告保存失败：{error}", file=sys.stderr, flush=True)
                    return None
