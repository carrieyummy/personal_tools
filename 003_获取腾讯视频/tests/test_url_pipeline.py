"""用回环 HTTP 和合成媒体验证完整 URL 下载、封装、发布、恢复及复用，不访问真实账号。"""

from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
from threading import Thread, Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from local.batch import load_index
from local.playback.application import UrlBatch, task_index
from local.playback.audio import AudioOption
from local.playback.client import NativeClient
from local.playback.errors import PlaybackError, SourceChanged
from local.playback.hls import parse_media
from local.playback.source import Resolved
from local.playback.transport import Transport
from local.runtime import TaskCancelled
from local.storage import atomic_json, identity_digest
from tests.test_batch import MemoryJournal
from tests.test_playback import current_config


class Handler(SimpleHTTPRequestHandler):
    """测试回环文件服务；统计实际下载次数，不输出访问日志。"""

    def do_GET(self):
        """响应当前测试 GET 并递增服务计数；无参数或返回值。"""
        self.server.requests += 1
        super().do_GET()

    def log_message(self, format, *args):
        """屏蔽 format/args 描述的访问日志，避免干扰测试摘要；无返回值。"""
        return


class UrlPipelineTests(unittest.TestCase):
    """真实媒体检查与可控来源并用，验证公共流水线边界。"""

    @classmethod
    def setUpClass(cls):
        """用配置指定的 FFmpeg 生成短 HLS 样本并开启回环服务；不下载媒体。"""
        cls.temporary = tempfile.TemporaryDirectory(prefix="tencent_media_")
        cls.root = Path(cls.temporary.name).resolve()
        cls.base = current_config()
        media = cls.root / "media"
        media.mkdir()
        command = [str(cls.base["ffmpeg_path"]), "-hide_banner", "-nostdin", "-loglevel", "error",
                   "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=12", "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=48000",
                   "-t", "2", "-c:v", "libx264", "-threads", "1", "-g", "12", "-pix_fmt", "yuv420p", "-c:a", "aac",
                   "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod",
                   "-hls_segment_filename", str(media / "segment_%03d.ts"), str(media / "sample.m3u8")]
        result = subprocess.run(command, capture_output=True, timeout=cls.base["process_timeout_seconds"])
        if result.returncode:
            cls.temporary.cleanup()
            raise RuntimeError(result.stderr.decode(errors="replace"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(media)))
        cls.server.requests = 0
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        """回收回环服务和本类临时样本；无参数或返回值。"""
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temporary.cleanup()

    def setUp(self):
        """为每例创建独立输出与断点配置，固定 CPU 作为隔离验证设备，不依赖机器 GPU。"""
        self.directory = tempfile.TemporaryDirectory(prefix="tencent_url_")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name).resolve()
        self.config = deepcopy(self.base)
        self.config.update(output_path=root / "output", work_dir=root / "work")
        self.config["decode_verification"] = {"cpu": True, "gpu": False}
        self.config["network"]["proxy"] = ""  # 回环测试强制直连，隔离用户实网代理。
        self.task = {"source_type": "url", "series_id": "cover", "video_id": "video", "series": "合成剧",
                     "title": "合成集", "index": 1, "quality": self.config["download"]["quality"], "note": "", "release_date": "2020-02-03"}
        content = (self.root / "media/sample.m3u8").read_text(encoding="utf-8")
        manifest = parse_media(content, f"http://127.0.0.1:{self.server.server_port}/sample.m3u8")
        audio = AudioOption("original", "原始音轨", False, True, 0, "original")
        description = {"source_type": "url", "format": {"id": 1, "name": "synthetic", "sname": "合成画质", "width": 96, "height": 64, "vfps": 12},
                       "duration_seconds": manifest.duration, "audio": audio.describe(), "audio_options": [audio.describe()]}
        self.source = Resolved(description, [manifest], audio, identity_digest(description))

    def batch(self) -> UrlBatch:
        """创建使用真实 HTTP 传输和内存日志的批次；返回无真实客户端登录依赖的执行器。"""
        transport = Transport(self.config, Event())
        return UrlBatch(self.config, MemoryJournal(), client=SimpleNamespace(transport=transport))

    def test_complete_publish_and_sha256_reuse_preserve_old(self):
        """完整下载/解码后登记 URL 成品；缓存旧版和未知同名文件保留，第二次运行只复用摘要一致的成品。"""
        index = task_index(self.config, self.task)
        index.parent.mkdir(parents=True)
        old = index.parent / "old_cache.mp4"
        old.write_bytes(b"old cached output")
        atomic_json(index, {"version": 1, "files": [{"source_type": "download", "video_id": self.task["video_id"],
                    "file": old.name, "source_identity": self.source.identity, "verification": {"exit_code": 0}}]})
        with patch("local.playback.application.resolve", return_value=self.source):
            first = self.batch().run([self.task])
            self.assertEqual(first["completed"], 1)
            count = self.server.requests
            second = self.batch().run([self.task])
        self.assertEqual(second["reused"], 1)
        self.assertEqual(self.server.requests, count)
        self.assertEqual(old.read_bytes(), b"old cached output")
        records = load_index(index)["files"]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1]["mtime_ns"], (index.parent / records[-1]["file"]).stat().st_mtime_ns)
        self.assertEqual(records[-1]["media"]["fps"], self.source.description["format"]["vfps"])
        self.assertTrue(records[-1]["media"]["audio_streams"])

    def test_queue_size_matches_downloaded_segment_files(self):
        """等待队列的大小来自实际视频和音轨分片，而不是清单估计或工作目录的其他文件。"""
        batch = self.batch()
        steps = batch._job_steps(self.task, batch.journal.add(self.task))
        with patch("local.playback.application.resolve", return_value=self.source):
            try:
                self.assertEqual(next(steps), ("download", 0))
                stage, size = next(steps)
                work = self.config["work_dir"] / "url" / self.task["series_id"] / self.task["video_id"] / self.source.identity
                actual = sum(path.stat().st_size for path in work.glob("track_*/*.bin"))
                self.assertEqual(stage, "ready")
                self.assertGreater(actual, 0)
                self.assertEqual(size, actual)
            finally:
                steps.close()

    def test_validation_failure_keeps_resume_and_retry_uses_it(self):
        """验证失败不得发布；重跑复用逐片 SHA256 通过的断点，不重复网络下载，媒体日志使用独立目录。"""
        with patch("local.playback.application.resolve", return_value=self.source):
            with patch.object(UrlBatch, "verify", side_effect=PlaybackError("synthetic validation failed")):
                first = self.batch().run([self.task])
            self.assertEqual(first["failed"], 1)
            self.assertFalse(task_index(self.config, self.task).exists())
            count = self.server.requests
            second = self.batch().run([self.task])
        self.assertEqual(second["completed"], 1)
        self.assertEqual(self.server.requests, count)

    def test_source_change_never_publishes(self):
        """完成验证后的来源变化阻止发布，且不把失败现场当作完成成品。"""
        calls = 0
        def changed(*args, **kwargs):
            """前两次返回固定来源，后续抛版本变化；args/kwargs 为实际来源接口参数。"""
            nonlocal calls
            calls += 1
            if calls > 2:
                raise SourceChanged("synthetic changed source")
            return self.source
        with patch("local.playback.application.resolve", side_effect=changed):
            result = self.batch().run([self.task])
        self.assertEqual(result["failed"], 1)
        self.assertFalse(task_index(self.config, self.task).exists())
        self.assertFalse(list(self.config["output_path"].rglob("*.mp4")))

    def test_index_failure_rolls_back_only_new_publication(self):
        """索引写入失败时删除本次新发布文件，保留候选与断点以便恢复。"""
        index = task_index(self.config, self.task)
        def reject(path, value):
            """只拒绝目标索引 path，其余 value 按真实原子写入处理；无返回值。"""
            if path == index:
                raise OSError("synthetic index failure")
            return atomic_json(path, value)
        with patch("local.playback.application.resolve", return_value=self.source), \
             patch("local.playback.application.atomic_json", side_effect=reject):
            result = self.batch().run([self.task])
        self.assertEqual(result["failed"], 1)
        self.assertFalse(index.exists())
        self.assertFalse(list(self.config["output_path"].rglob("*.mp4")))

    def test_pre_cancelled_job_never_downloads(self):
        """预先取消的任务直接结束，不读取账号、联网或创建成品。"""
        batch = self.batch()
        batch.cancel.set()
        count = self.server.requests
        with self.assertRaises(TaskCancelled):
            batch.job(self.task, batch.journal.add(self.task))
        self.assertEqual(self.server.requests, count)

    def test_query_range_cannot_download_whole_file(self):
        """服务端忽略腾讯查询范围并返回整片时立即失败，不把整片误当作分片写入并发布。"""
        from dataclasses import replace
        playlist = self.source.manifests[0]
        playlist.resources[0] = replace(playlist.resources[0], url=playlist.resources[0].url + "?index=0&brs=0&bre=99")
        with patch("local.playback.application.resolve", return_value=self.source):
            result = self.batch().run([self.task])
        self.assertEqual(result["failed"], 1)
        self.assertFalse(task_index(self.config, self.task).exists())

    def authentication_batch(self, accepted_after_refresh: bool) -> tuple[UrlBatch, Mock]:
        """构造复核阶段认证替身；accepted_after_refresh 控制重读后是否恢复，返回批次及原始请求 Mock。"""
        credentials = {"tokens": {"vuserid": "synthetic-account", "vusession": "old-session"}, "app_version": "test"}
        client = NativeClient(self.config, user=credentials)
        client.authentication.loader = Mock(return_value={**credentials, "tokens": {**credentials["tokens"], "vusession": "new-session"}})
        settings = self.config["playback"]["playback"]
        def response(*args, **kwargs):
            """为 args/kwargs 的同源请求按当前登录代次返回最小授权响应。"""
            accepted = accepted_after_refresh and kwargs["snapshot"].generation > 1
            return {"code": float(settings["success_codes"][0]),
                    "login": settings["authenticated_states"][0] if accepted else -1,
                    "vl": {"vi": [{"vid": self.task["video_id"]}]}}
        client.raw_source = Mock(side_effect=response)
        return UrlBatch(self.config, MemoryJournal(), client=client), client.raw_source

    def test_authentication_refresh_after_decode_continues_publication(self):
        """下载解码完成后复核遭拒时重读恢复，直接继续发布，不重新下载或降档。"""
        batch, raw = self.authentication_batch(True)
        def resolved(client, task, selector, *, expected=None):
            """首轮直接返回样本，expected 存在时用 client/task 复核认证；selector 保留正式接口。"""
            if expected is not None:
                client.source(task["series_id"], task["video_id"], "same-definition", {"audio_track": "same-audio"})
            return self.source
        before = self.server.requests
        with patch("local.playback.application.resolve", side_effect=resolved):
            result = batch.run([self.task])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.server.requests - before, len(self.source.manifests[0].resources))
        self.assertEqual(batch.authentication.diagnostics()["recoveries"], 1)
        self.assertEqual(raw.call_count, 3)  # 一次被拒、一次恢复，以及发布前再复核。
        self.assertTrue(task_index(self.config, self.task).exists())
        self.assertEqual(batch.journal.entries[0]["authentication"]["recoveries"], 1)

    def test_authentication_failure_after_decode_keeps_download_and_candidate(self):
        """复核认证恢复耗尽时不发布，完整分片和解码后的候选文件保留供重新运行。"""
        batch, raw = self.authentication_batch(False)
        def resolved(client, task, selector, *, expected=None):
            """只在 expected 指定的复核阶段通过 client 请求 task；selector 保留正式接口，返回样本。"""
            if expected is not None:
                client.source(task["series_id"], task["video_id"])
            return self.source
        with patch("local.playback.application.resolve", side_effect=resolved):
            result = batch.run([self.task])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(raw.call_count, 1 + self.config["authentication"]["recovery_attempts"])
        self.assertFalse(task_index(self.config, self.task).exists())
        self.assertTrue(list(self.config["work_dir"].rglob("segments.json")))
        self.assertTrue(list(self.config["work_dir"].rglob("candidate_*.partial.mp4")))
