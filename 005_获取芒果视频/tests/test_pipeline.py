"""以阶段替身和真实同步验证资源上限、跨阶段重叠、取消与发布隔离；不以此替代实网媒体验收。"""

from threading import Event, Lock, current_thread
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from local.application import Pipeline
from local.errors import AuthenticationError, Cancelled, SourceChanged, ToolError
from local.scheduler import run_pipeline
from local.tasks import Episode
from tests.test_workflow import BaseCase


class PipelineTests(BaseCase):
    """从当前主配置取得并发上限，模拟下载/封装/验证/发布的资源占用。"""

    def pipeline(self):
        """建立隔离 Pipeline；无参数；返回使用替身服务的执行器。"""
        publisher = Mock()
        publisher.reusable.return_value = None
        publisher.publish.side_effect = lambda source, path, *args: {"path": path.as_posix()}
        return Pipeline(self.config, Mock(), Mock(), publisher, self.cancel, Event())

    def execute(self, pipeline, items):
        """通过正式两池调度 pipeline 的 items；返回终态列表，错误按正式阶段结算。"""
        statuses = []

        def complete(episode, status, error):
            """结算 episode 的 status/error 并保存终态；无返回值。"""
            statuses.append(pipeline.failure(episode, error) if error is not None else status)

        run_pipeline(items, self.config, pipeline.episode_steps, complete,
                     self.cancel, pipeline.auth_stop, downloaded_size=lambda task: task.evidence["bytes"], waiting=pipeline.stage)
        return statuses

    def test_stage_limits_and_overlap(self):
        """同一批任务必须遵守每个配置槽且能跨阶段重叠；无参数/返回值。"""
        pipeline = self.pipeline()
        lock = Lock()
        active, maximum = {}, {}
        overlapped = []

        def occupancy(stage, result):
            """模拟资源阶段；stage 为资源名，result 为返回值；返回占用一段时间后的结果。"""
            with lock:
                if any(count and name != stage for name, count in active.items()):
                    overlapped.append(True)
                active[stage] = active.get(stage, 0) + 1
                maximum[stage] = max(maximum.get(stage, 0), active[stage])
                if stage != "download":
                    total = sum(count for name, count in active.items() if name != "download")
                    maximum["processing_total"] = max(maximum.get("processing_total", 0), total)
            time.sleep(self.config["runtime"]["cancel_poll_interval_seconds"] * 2)
            with lock:
                active[stage] -= 1
            return result

        stream = {"fileId": "fixture", "fileHash": "fixture_hash", "definition": "fixture", "ftime": "1", "standardName": "测试画质",
                  "videoWidth": "64", "videoHeight": "64", "videoFormat": "H264", "disp": {"info": "http://fixture/index.m3u8"}}
        media = [SimpleNamespace(duration=1, identity="playlist")]

        def downloading(transport, initial, directory, config, progress, cancel):
            """模拟下载；参数与生产下载一致；返回本地清单和媒体证据。"""
            self.assertTrue(current_thread().name.startswith("mgtv-download"))
            yield from ()
            return occupancy("download", ([directory / "local.m3u8"], {"durations": [1], "bytes": 1}))

        def muxing(config, manifests, candidate, cancel):
            """模拟封装；参数与生产封装一致；写候选文件并返回 None。"""
            occupancy("mux", None)
            candidate.write_bytes(b"fixture")

        def verifying(config, candidate, device, durations, source, cancel):
            """模拟验证；参数与生产验证一致；返回验证证据。"""
            return occupancy(device, {"device": device})

        original_publish = pipeline.publisher.publish.side_effect
        pipeline.publisher.publish.side_effect = lambda *args: occupancy("publish", original_publish(*args))
        count = self.config["max_in_flight"] + 1
        items = [Episode("1", str(index), index, f"测试{index}", "测试", True, 1, release_date="2000-01-02") for index in range(1, count + 1)]
        with patch("local.application.resolved", side_effect=lambda *args: occupancy("resolve", stream)), patch("local.application.playlists", return_value=media), \
                patch("local.application.download_steps", side_effect=downloading), patch("local.application.mux", side_effect=muxing), patch("local.application.verify", side_effect=verifying):
            statuses = self.execute(pipeline, items)
        self.assertEqual(statuses, ["成功"] * count)
        self.assertLessEqual(maximum["download"], self.config["download"]["threads"])
        self.assertLessEqual(maximum["processing_total"], self.config["max_in_flight"])
        self.assertLessEqual(maximum["mux"], self.config["pipeline"]["mux_slots"])
        self.assertLessEqual(maximum["publish"], self.config["pipeline"]["publish_slots"])
        for device in ("cpu", "gpu"):
            self.assertLessEqual(maximum.get(device, 0), int(self.config["decode_verification"][device]))
        if self.config["max_in_flight"] > 1:
            self.assertTrue(overlapped)

    def test_authentication_stop_and_cancellation(self):
        """登录失效阻止后续请求，取消时不得发布；无参数/返回值。"""
        pipeline = self.pipeline()
        item = Episode("1", "2", 1, "测试", "测试", True, 1)
        with patch("local.application.resolved", side_effect=AuthenticationError("过期")) as request:
            self.assertEqual(self.execute(pipeline, [item]), ["认证失效"])
            with self.assertRaises(StopIteration) as stopped:
                next(pipeline.episode_steps(item))
            self.assertEqual(stopped.exception.value, "认证失效")
            self.assertEqual(request.call_count, 1)
        pipeline.publisher.publish.assert_not_called()
        pipeline = self.pipeline()
        self.cancel.set()
        with self.assertRaises(Cancelled):
            self.execute(pipeline, [item])
        pipeline.publisher.publish.assert_not_called()

    def test_expired_address_refresh_uses_shared_processing_pool(self):
        """正式任务遇到地址过期时释放下载池，续签在处理池执行，回到下载池后完成后处理。"""
        pipeline = self.pipeline()
        item = Episode("1", "2", 1, "测试", "测试", True, 1, release_date="2000-01-02")
        stream = {"fileId": "fixture", "fileHash": "hash", "definition": "fixture", "ftime": "1", "standardName": "测试",
                  "videoWidth": "64", "videoHeight": "64", "videoFormat": "H264", "disp": {"info": "http://fixture/index.m3u8"}}
        media, calls = [SimpleNamespace(duration=1, identity="playlist")], []

        def resolve(*args):
            """为 args 对应的正片返回同源媒体，检查解析及续签均使用共享处理池。"""
            self.assertTrue(current_thread().name.startswith("mgtv-process"))
            calls.append("resolve")
            return stream

        def transfer(transport, initial, directory, config, progress, cancel):
            """模拟 initial 首次下载过期并接收续签清单；其他参数遵循正式签名，返回媒体路径与证据。"""
            self.assertTrue(current_thread().name.startswith("mgtv-download"))
            calls.append("download")
            renewed = yield "refresh"
            self.assertEqual(renewed, media)
            self.assertTrue(current_thread().name.startswith("mgtv-download"))
            calls.append("download")
            return [], {"durations": [1], "bytes": 1}

        def process(task):
            """检查 task 的后处理使用共享池；返回成功终态，不调用真实媒体进程。"""
            self.assertTrue(current_thread().name.startswith("mgtv-process"))
            calls.append("process")
            return "成功"

        with patch("local.application.resolved", side_effect=resolve), patch("local.application.playlists", return_value=media), \
                patch("local.application.download_steps", side_effect=transfer), patch.object(pipeline, "process_episode", side_effect=process):
            self.assertEqual(self.execute(pipeline, [item]), ["成功"])
        self.assertEqual(calls, ["resolve", "download", "resolve", "download", "process"])

    def test_verification_failure_never_publishes(self):
        """候选文件已生成但完整验证失败时保留现场且不提交；无参数/返回值。"""
        pipeline = self.pipeline()
        item = Episode("1", "2", 1, "测试", "测试", True, 1)
        stream = {"fileId": "fixture", "fileHash": "hash", "definition": "fixture", "ftime": "1", "standardName": "测试",
                  "videoWidth": "64", "videoHeight": "64", "videoFormat": "H264", "disp": {"info": "http://fixture/index.m3u8"}}

        def candidate(config, manifests, path, cancel):
            """构造未验证文件；参数同生产封装；无返回值。"""
            path.write_bytes(b"unverified")

        def downloaded(*args):
            """模拟 args 指定的完整媒体传输，返回本地清单及字节证据。"""
            yield from ()
            return [], {"durations": [1], "bytes": 1}

        with patch("local.application.resolved", return_value=stream), patch("local.application.playlists", return_value=[SimpleNamespace(duration=1, identity="playlist")]), \
                patch("local.application.download_steps", side_effect=downloaded), patch("local.application.mux", side_effect=candidate), \
                patch("local.application.verify", side_effect=ToolError("解码失败")):
            self.assertEqual(self.execute(pipeline, [item]), ["失败"])
        pipeline.publisher.publish.assert_not_called()
        self.assertTrue(list(self.config["work_dir"].rglob("candidate.mp4")))

    @staticmethod
    def reused_steps():
        """返回模拟复用结果的阶段生成器；不发起下载。"""
        yield from ()
        return "复用"

    def test_source_change_restarts_without_discarding_old_work(self):
        """源变化只重试新版本，未达到上限时允许继续；无参数/返回值。"""
        pipeline = self.pipeline()
        item = Episode("1", "2", 1, "测试", "测试", True, 1)
        with patch.object(pipeline, "transfer", side_effect=[SourceChanged("更新"), self.reused_steps()]) as attempt:
            self.assertEqual(self.execute(pipeline, [item]), ["复用"])
            self.assertEqual(attempt.call_count, 2)

    def test_continuously_changed_source_stops_at_configured_limit(self):
        """持续源变化按配置停止，禁止入队和发布；无参数/返回值。"""
        pipeline = self.pipeline()
        item = Episode("1", "2", 1, "测试", "测试", True, 1)
        with patch.object(pipeline, "transfer", side_effect=SourceChanged("持续更新")) as attempt:
            self.assertEqual(self.execute(pipeline, [item]), ["失败"])
        self.assertEqual(attempt.call_count, self.config["download"]["refresh_attempts"] + 1)
        pipeline.publisher.publish.assert_not_called()
