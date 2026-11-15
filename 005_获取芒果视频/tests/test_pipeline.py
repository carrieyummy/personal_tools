"""以阶段替身和真实同步验证资源上限、跨阶段重叠、取消与发布隔离；不以此替代实网媒体验收。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from local.application import Pipeline
from local.errors import AuthenticationError, Cancelled, SourceChanged, ToolError
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
            time.sleep(self.config["runtime"]["cancel_poll_interval_seconds"] * 2)
            with lock:
                active[stage] -= 1
            return result

        stream = {"fileId": "fixture", "fileHash": "fixture_hash", "definition": "fixture", "ftime": "1", "standardName": "测试画质",
                  "videoWidth": "64", "videoHeight": "64", "videoFormat": "H264", "disp": {"info": "http://fixture/index.m3u8"}}
        media = [SimpleNamespace(duration=1, identity="playlist")]

        def downloading(transport, initial, directory, config, refresh, progress, cancel):
            """模拟下载；参数与生产下载一致；返回本地清单和媒体证据。"""
            return occupancy("download", ([directory / "local.m3u8"], {"durations": [1]}))

        def muxing(config, manifests, candidate, cancel):
            """模拟封装；参数与生产封装一致；写候选文件并返回 None。"""
            occupancy("mux", None)
            candidate.write_bytes(b"fixture")

        def verifying(config, candidate, device, durations, source, cancel):
            """模拟验证；参数与生产验证一致；返回验证证据。"""
            return occupancy(device, {"device": device})

        original_publish = pipeline.publisher.publish.side_effect
        pipeline.publisher.publish.side_effect = lambda *args: occupancy("publish", original_publish(*args))
        count = self.config["pipeline"]["max_in_flight"] + 1
        items = [Episode("1", str(index), index, f"测试{index}", "测试", True, 1) for index in range(1, count + 1)]
        with patch("local.application.resolved", return_value=stream), patch("local.application.playlists", return_value=media), \
                patch("local.application.download", side_effect=downloading), patch("local.application.mux", side_effect=muxing), patch("local.application.verify", side_effect=verifying):
            with ThreadPoolExecutor(max_workers=self.config["pipeline"]["max_in_flight"]) as executor:
                statuses = list(executor.map(pipeline.item, items))
        self.assertEqual(statuses, ["成功"] * count)
        self.assertLessEqual(maximum["download"], self.config["download"]["threads"])
        self.assertLessEqual(maximum["mux"], self.config["pipeline"]["mux_slots"])
        self.assertLessEqual(maximum["publish"], self.config["pipeline"]["publish_slots"])
        for device in ("cpu", "gpu"):
            self.assertLessEqual(maximum.get(device, 0), int(self.config["decode_verification"][device]))
        if self.config["pipeline"]["max_in_flight"] > 1:
            self.assertTrue(overlapped)

    def test_authentication_stop_and_cancellation(self):
        """登录失效阻止后续请求，取消时不得发布；无参数/返回值。"""
        pipeline = self.pipeline()
        item = Episode("1", "2", 1, "测试", "测试", True, 1)
        with patch("local.application.resolved", side_effect=AuthenticationError("过期")) as request:
            self.assertEqual(pipeline.item(item), "认证失效")
            self.assertEqual(pipeline.item(item), "认证失效")
            self.assertEqual(request.call_count, 1)
        pipeline.publisher.publish.assert_not_called()
        pipeline = self.pipeline()
        self.cancel.set()
        self.assertEqual(pipeline.item(item), "取消")
        pipeline.publisher.publish.assert_not_called()

    def test_verification_failure_never_publishes(self):
        """候选文件已生成但完整验证失败时保留现场且不提交；无参数/返回值。"""
        pipeline = self.pipeline()
        item = Episode("1", "2", 1, "测试", "测试", True, 1)
        stream = {"fileId": "fixture", "fileHash": "hash", "definition": "fixture", "ftime": "1", "standardName": "测试",
                  "videoWidth": "64", "videoHeight": "64", "videoFormat": "H264", "disp": {"info": "http://fixture/index.m3u8"}}

        def candidate(config, manifests, path, cancel):
            """构造未验证文件；参数同生产封装；无返回值。"""
            path.write_bytes(b"unverified")

        with patch("local.application.resolved", return_value=stream), patch("local.application.playlists", return_value=[SimpleNamespace(duration=1, identity="playlist")]), \
                patch("local.application.download", return_value=([], {"durations": [1]})), patch("local.application.mux", side_effect=candidate), \
                patch("local.application.verify", side_effect=ToolError("解码失败")):
            self.assertEqual(pipeline.item(item), "失败")
        pipeline.publisher.publish.assert_not_called()
        self.assertTrue(list(self.config["work_dir"].rglob("candidate.mp4")))

    def test_source_change_restarts_without_discarding_old_work(self):
        """源变化只重试新版本，未达到上限时允许继续；无参数/返回值。"""
        pipeline = self.pipeline()
        item = Episode("1", "2", 1, "测试", "测试", True, 1)
        with patch.object(pipeline, "attempt", side_effect=[SourceChanged("更新"), "成功"]) as attempt:
            self.assertEqual(pipeline.item(item), "成功")
            self.assertEqual(attempt.call_count, 2)
