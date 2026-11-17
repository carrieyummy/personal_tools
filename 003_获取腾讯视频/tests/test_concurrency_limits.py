"""以当前配置的同步屏障验证解析不占下载名额，下载满载时仍可解析下一批任务。"""
from threading import Event, Lock, current_thread
from concurrent.futures import ThreadPoolExecutor
import unittest
from types import SimpleNamespace
from pathlib import Path
import tempfile
from unittest.mock import patch
from local.playback.application import UrlBatch
from local.playback.transport import Transport
from tests.test_batch import MemoryJournal
from tests.test_playback import current_config


class ConcurrencyLimitsTests(unittest.TestCase):
    """根据配置取得下载和处理上限，使用事件而非固定延时观察占用。"""

    def test_expired_address_returns_to_processing_pool(self):
        """正式 URL 任务遇到地址过期时，将续签转给共享处理池，再返回下载池继续。"""
        config = current_config()
        with tempfile.TemporaryDirectory() as temporary:
            config["work_dir"], config["output_path"] = Path(temporary) / "work", Path(temporary) / "output"
            batch = UrlBatch(config, MemoryJournal(), client=SimpleNamespace(transport=Transport(config, Event())))
            calls = []
            source = SimpleNamespace(identity="fixture", description={"format": {"sname": "fixture"}},
                                     audio=SimpleNamespace(name="fixture", metadata={}),
                                     manifests=[SimpleNamespace(resources=[None])])

            def resolve(*args, **kwargs):
                """为 args/kwargs 指定目标返回固定来源，检查初始解析及续签的线程池。"""
                self.assertTrue(current_thread().name.startswith("tencent-process"))
                calls.append("resolve")
                return source

            def transfer(transport, initial, work, config, progress, cancel):
                """模拟 initial 媒体过期后继续下载至 work；其他参数遵循正式签名，返回清单及证据。"""
                self.assertTrue(current_thread().name.startswith("tencent-download"))
                calls.append("download")
                fresh = yield "refresh"
                self.assertEqual(fresh, initial)
                self.assertTrue(current_thread().name.startswith("tencent-download"))
                calls.append("download")
                track = work / "track_0"
                track.mkdir()
                (track / "000000.bin").write_bytes(b"x")
                return [track / "local.m3u8"], {}

            def process(*args):
                """检查 args 对应的封装在共享池运行，主动结束测试而不调用真实媒体工具。"""
                self.assertTrue(current_thread().name.startswith("tencent-process"))
                calls.append("process")
                raise ValueError("fixture stops before media processing")

            with patch("local.playback.application.resolve", side_effect=resolve), \
                    patch("local.playback.application.reusable", return_value=None), \
                    patch("local.playback.application.download_steps", side_effect=transfer), \
                    patch.object(batch, "mux_manifests", side_effect=process):
                self.assertEqual(batch.run([{"series_id": "fixture", "video_id": "fixture", "title": "fixture"}])["failed"], 1)
            self.assertEqual(calls, ["resolve", "download", "resolve", "download", "process"])

    def test_downloads_overlap_with_separate_bounded_resolution(self):
        """下载池达到配置上限时，解析在共享处理池继续；释放后所有任务完成。"""
        config = current_config()
        downloads, processing = config["download"]["threads"], config["max_in_flight"]
        overlap = min(downloads, processing)
        full, parsing, release, lock = Event(), Event(), Event(), Lock()
        active, peaks, done = [0, 0], [0, 0], []

        def steps(index):
            """解析 index 后转入下载，最后交回处理池；返回模拟成功报告。"""
            self.assertTrue(current_thread().name.startswith("tencent-process"))
            with lock:
                active[1] += 1
                peaks[1] = max(peaks[1], active[1])
                if index >= downloads and active[1] == overlap:
                    parsing.set()
            try:
                if index >= downloads:
                    self.assertTrue(full.wait(10))
                    self.assertTrue(release.wait(10))
            finally:
                with lock:
                    active[1] -= 1
            yield "download", 0
            self.assertTrue(current_thread().name.startswith("tencent-download"))
            with lock:
                active[0] += 1
                peaks[0] = max(peaks[0], active[0])
                if active[0] == downloads:
                    full.set()
            try:
                self.assertTrue(release.wait(10))
            finally:
                with lock:
                    active[0] -= 1
            yield "ready", 1
            self.assertTrue(current_thread().name.startswith("tencent-process"))
            done.append(index)
            return {"status": "completed"}

        batch = UrlBatch(config, MemoryJournal(), client=SimpleNamespace(transport=Transport(config, Event())))

        def run():
            """用生产 URL 调度运行合成剧集，无返回值。"""
            with patch.object(batch, "_job_steps", side_effect=lambda task, entry: steps(int(task["video_id"]))):
                result = batch.run([{"video_id": str(index), "title": str(index)} for index in range(downloads + overlap)])
                self.assertEqual(result["completed"], downloads + overlap)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run)
            try:
                self.assertTrue(full.wait(5))
                self.assertTrue(parsing.wait(5))
                with lock:
                    self.assertEqual(active, [downloads, overlap])
            finally:
                release.set()
            future.result(timeout=15)
        self.assertEqual(len(done), downloads + overlap)
        self.assertEqual(peaks[0], downloads)
        self.assertLessEqual(peaks[1], processing)
