"""通过事件协调验证流水线重叠、设备限制、索引提交隔离及取消，不依赖具体视频和配置取值。"""

from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from threading import Event, Lock
from unittest.mock import patch

from qsv_local.batch import Pipeline, run_batch
from qsv_local.configuration import read_config
from qsv_local.download_state import DownloadNotReady
from qsv_local.runtime import TaskCancelled
from qsv_local.storage import atomic_json, fingerprint, publish, read_json
from tests.support import ProjectCase


class DeviceConfigurationTests(ProjectCase):
    """验证设备配置的三种有效组合及明确错误，不依赖主配置的当前开关值。"""

    def test_valid_device_modes(self):
        """显式 CPU、GPU 及混合三种配置均可读取；无参数和返回值。"""
        for devices in ({"cpu": True, "gpu": False}, {"cpu": False, "gpu": True}, {"cpu": True, "gpu": True}):
            with self.subTest(devices=devices):
                self.raw["decode_verification"] = devices
                self.write_config()
                self.assertEqual(read_config(self.config_path)["decode_verification"], devices)

    def test_invalid_device_modes(self):
        """拒绝关闭全部设备、字段缺失、整数或字符串代替布尔值及缺失整个配置块；无参数和返回值。"""
        for devices in ({"cpu": False, "gpu": False}, {"cpu": True}, {}, None,
                        {"cpu": 1, "gpu": False}, {"cpu": True, "gpu": "true"}):
            with self.subTest(devices=devices):
                self.raw["decode_verification"] = devices
                self.write_config()
                with self.assertRaisesRegex(ValueError, "decode_verification"):
                    read_config(self.config_path)
        del self.raw["decode_verification"]
        self.write_config()
        with self.assertRaisesRegex(ValueError, "decode_verification"):
            read_config(self.config_path)


class PipelineTests(ProjectCase):
    """用合成媒体执行真实调度和索引写盘；事件超时仅用于保护测试协调，不充当业务运行参数。"""

    def signal(self, event: Event) -> None:
        """等待测试 event，最多十秒；超时使测试失败以避免死锁，无返回值。"""
        self.assertTrue(event.wait(10), "测试阶段协调超时")

    def test_configured_inflight_limit_applies_to_local_batch(self):
        """本地转换按配置限制在途数量，设为一时逐项完成；无返回值。"""
        self.config["max_in_flight"] = 1
        self.source("series/one.qsv")
        self.source("series/two.qsv")
        counts, original = [], Pipeline.admit

        def admit(pipeline):
            """执行 pipeline 原接纳逻辑并记录在途数；无返回值。"""
            original(pipeline)
            counts.append(len(pipeline.jobs))

        with patch.object(Pipeline, "admit", admit):
            result, _ = self.batch()
        self.assertEqual((result["completed"], result["failed"]), (2, 0))
        self.assertTrue(counts)
        self.assertLessEqual(max(counts), self.config["max_in_flight"])

    def test_mux_overlaps_next_preparation(self):
        """首项封装等待下一项准备，证明 SDK 槽在封装期间可复用；无参数或返回值。"""
        self.source("series/one.qsv")
        self.source("series/two.qsv")
        mux_started, second_prepared = Event(), Event()
        original_prepare = self.fake_prepare

        def prepare(config, source, work, notify, cancel_event):
            """使用正式参数准备 source；第二项等待首项封装，返回提取报告。"""
            if source.stem == "two":
                self.signal(mux_started)
                second_prepared.set()
            return original_prepare(config, source, work, notify, cancel_event)

        def mux(config, report, work, notify, cancel_event):
            """首项等待第二项准备；参数遵循封装接口，返回待验证报告。"""
            if work.name == "one.qsv":
                mux_started.set()
                self.signal(second_prepared)
            return self.fake_mux(config, report, work, notify, cancel_event)

        with patch.object(self, "fake_prepare", side_effect=prepare), patch("qsv_local.batch.mux", side_effect=mux):
            result, _ = self.batch()
        self.assertEqual((result["completed"], result["failed"]), (2, 0))

    def test_decode_slot_is_reused_during_postcheck(self):
        """首项后置检查等待第二项解码，证明设备槽及时释放；无参数或返回值。"""
        self.config["decode_verification"] = {"cpu": True, "gpu": False}
        self.source("series/one.qsv")
        self.source("series/two.qsv")
        checking, second_decoded = Event(), Event()
        original_verify = self.fake_verify

        def verify(config, report, work, notify, device, cancel_event):
            """第二项等待首项检查开始；参数遵循解码接口，返回解码报告。"""
            if work.name == "two.qsv":
                self.signal(checking)
                second_decoded.set()
            return original_verify(config, report, work, notify, device, cancel_event)

        def finalize(config, report, work, notify, cancel_event):
            """首项等待第二项解码；参数遵循检查接口，返回完整证明。"""
            if work.name == "one.qsv":
                checking.set()
                self.signal(second_decoded)
            return self.fake_finalize(config, report, work, notify, cancel_event)

        with patch.object(self, "fake_verify", side_effect=verify), patch("qsv_local.batch.finalize_verification", side_effect=finalize):
            result, _ = self.batch()
        self.assertEqual((result["completed"], result["failed"]), (2, 0))

    def test_slow_fingerprint_does_not_block_commit(self):
        """第二项指纹等待首项提交，证明调度线程不会被指纹读取阻塞；无参数或返回值。"""
        self.source("series/one.qsv")
        second = self.source("series/two.qsv")
        hashing, committed = Event(), Event()
        original_commit = Pipeline.commit
        original_verify = self.fake_verify

        def delayed_fingerprint(path, buffer_bytes, cached=None, **kwargs):
            """第二项输入指纹等待首项提交；path、buffer_bytes、cached 和 kwargs 透传，返回实际指纹。"""
            if path == second:
                hashing.set()
                self.signal(committed)
            return fingerprint(path, buffer_bytes, cached, **kwargs)

        def verify(config, report, work, notify, device, cancel_event):
            """首项等待第二项指纹开始；其他参数遵循解码接口，返回解码证明。"""
            if work.name == "one.qsv":
                self.signal(hashing)
            return original_verify(config, report, work, notify, device, cancel_event)

        def commit(pipeline, job, result):
            """执行 pipeline 的原提交，job 和 result 原样转发；首项提交后发出信号，无返回值。"""
            original_commit(pipeline, job, result)
            if job.task.source.stem == "one":
                committed.set()

        with (patch("qsv_local.batch.fingerprint", side_effect=delayed_fingerprint),
              patch.object(self, "fake_verify", side_effect=verify), patch.object(Pipeline, "commit", commit)):
            result, _ = self.batch()
        self.assertEqual((result["completed"], result["failed"]), (2, 0))

    def test_source_changed_after_async_fingerprint_is_rechecked(self):
        """指纹完成后源内容变化时重新计算，避免使用过期结果；无参数或返回值。"""
        source = self.source()
        calls = []

        def changing_fingerprint(path, buffer_bytes, cached=None, **kwargs):
            """计算 path 指纹并在第一次返回前改写源文件；其余参数透传，返回原指纹。"""
            result = fingerprint(path, buffer_bytes, cached, **kwargs)
            if path == source:
                calls.append(result)
                if len(calls) == 1:
                    source.write_bytes(b"changed-source-content")
            return result

        with patch("qsv_local.batch.fingerprint", side_effect=changing_fingerprint):
            result, _ = self.batch()
        self.assertEqual((result["completed"], result["failed"]), (1, 0))
        self.assertGreaterEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["sha256"], calls[1]["sha256"])

    def test_direct_batch_call_with_no_device_is_rejected(self):
        """程序调用绕过配置读取时也拒绝空设备，避免待验证队列无法推进；无参数和返回值。"""
        self.config["decode_verification"] = {"cpu": False, "gpu": False}
        self.source()
        with redirect_stdout(self.console), self.assertRaisesRegex(ValueError, "decode_verification"):
            run_batch(self.config)

    def test_all_stages_overlap_with_bounded_inflight_and_single_device_slots(self):
        """同时观察准备、CPU、GPU 和发布四个阶段，验证各阶段唯一槽及在途总上限；无参数和返回值。"""
        self.config["decode_verification"] = {"cpu": True, "gpu": True}
        self.config["max_in_flight"] = 4  # 本用例需要观察四个同时在途的阶段。
        for number in range(8):
            self.source(f"series/video{number}.qsv")
        cpu_started, second_gpu, publishing, overlap = Event(), Event(), Event(), Event()
        lock, active, peaks, inflight = Lock(), Counter(), Counter(), []
        original_admit = Pipeline.admit

        def enter(name):
            """记录 name 阶段开始及峰值，在线程锁内更新，无返回值。"""
            with lock:
                active[name] += 1
                peaks[name] = max(peaks[name], active[name])

        def leave(name):
            """记录 name 阶段结束，在线程锁内更新，无返回值。"""
            with lock:
                active[name] -= 1

        def admit(pipeline):
            """执行 pipeline 原接纳逻辑并记录在途数量，无返回值。"""
            original_admit(pipeline)
            inflight.append(len(pipeline.jobs))

        def prepare(config, source, work, notify, cancel_event):
            """按正式 config/source/work/notify/cancel_event 接口准备；第四个任务证明其他三阶段仍活动，返回候选。"""
            enter("preparing")
            try:
                if source.stem == "video3":
                    self.signal(publishing)
                    self.signal(second_gpu)
                    with lock:
                        self.assertEqual([active[key] for key in ("cpu", "gpu", "publishing")], [1, 1, 1])
                    overlap.set()
                return self.fake_prepare(config, source, work, notify, cancel_event)
            finally:
                leave("preparing")

        def verify(config, report, work, notify, device, cancel_event):
            """按正式验证接口执行事件协调；config/report/work/notify/device/cancel_event 原样转发，返回验证证明。"""
            enter(device)
            try:
                title = work.name
                if title == "video0.qsv":
                    self.assertEqual(device, "gpu")
                    self.signal(cpu_started)
                elif title == "video1.qsv":
                    self.assertEqual(device, "cpu")
                    cpu_started.set()
                    self.signal(overlap)
                elif title == "video2.qsv":
                    self.assertEqual(device, "gpu")
                    second_gpu.set()
                    self.signal(overlap)
                return self.fake_verify(config, report, work, notify, device, cancel_event)
            finally:
                leave(device)

        def copy(source, target, digest, buffer_bytes, *, cancel_event=None):
            """以正式发布参数 source/target/digest/buffer_bytes/cancel_event 复制，首项等待重叠观察；返回成品指纹。"""
            enter("publishing")
            try:
                if target == self.target("series/video0.qsv"):
                    publishing.set()
                    self.signal(overlap)
                return publish(source, target, digest, buffer_bytes, cancel_event=cancel_event)
            finally:
                leave("publishing")

        with (patch("qsv_local.batch.prepare", side_effect=prepare), patch("qsv_local.batch.verify", side_effect=verify),
              patch("qsv_local.batch.publish", side_effect=copy), patch("qsv_local.batch.check_gpu"),
              patch.object(Pipeline, "admit", admit), redirect_stdout(self.console)):
            result = run_batch(self.config)
        self.assertEqual((result["completed"], result["failed"]), (8, 0), self.console.getvalue())
        self.assertTrue(overlap.is_set())
        self.assertEqual(dict(peaks), {"preparing": 1, "gpu": 1, "cpu": 1, "publishing": 1})
        self.assertLessEqual(max(inflight), self.config["max_in_flight"])
        self.assertEqual(len(read_json(self.target("series/video0.qsv").parent / "video_info.json")["files"]), 8)

    def test_cpu_only_still_prepares_next_file_during_verification(self):
        """只启用 CPU 时不检查 GPU，准备下一文件仍与当前验证重叠；无参数和返回值。"""
        self.config["decode_verification"] = {"cpu": True, "gpu": False}
        self.source("series/one.qsv")
        self.source("series/two.qsv")
        next_prepared = Event()

        def prepare(config, source, work, notify, cancel_event):
            """通过正式准备参数创建候选，第二个文件准备完成时通知；返回报告。"""
            report = self.fake_prepare(config, source, work, notify, cancel_event)
            if source.stem == "two":
                next_prepared.set()
            return report

        def verify(config, report, work, notify, device, cancel_event):
            """通过正式验证参数检查只使用 CPU，首项等待第二项准备；返回证明。"""
            self.assertEqual(device, "cpu")
            if work.name == "one.qsv":
                self.signal(next_prepared)
            return self.fake_verify(config, report, work, notify, device, cancel_event)

        with (patch("qsv_local.batch.prepare", side_effect=prepare), patch("qsv_local.batch.verify", side_effect=verify),
              patch("qsv_local.batch.check_gpu") as gpu_check, redirect_stdout(self.console)):
            result = run_batch(self.config)
        gpu_check.assert_not_called()
        self.assertEqual(result["completed"], 2)

    def test_gpu_only_does_not_dispatch_cpu(self):
        """只启用 GPU 时所有候选均使用 GPU；无参数和返回值。"""
        self.config["decode_verification"] = {"cpu": False, "gpu": True}
        self.source()
        with patch.object(self, "fake_verify", wraps=self.fake_verify) as verifier:
            result, _ = self.batch()
        self.assertEqual(result["completed"], 1)
        self.assertEqual(verifier.call_args.args[4], "gpu")
        index = read_json(self.target().parent / "video_info.json")
        self.assertEqual(index["files"][0]["verification"]["device"], "gpu")
        log = read_json(self.output / self.config["logging"]["state_file"])
        self.assertEqual(log["runs"][-1]["entries"][0]["verification"]["device"], "gpu")

    def test_download_guard_runs_before_publication_source_hash(self):
        """发布时下载状态改变，重新校验必须先于源摘要读取，跳过并保留候选；无参数和返回值。"""
        self.source()
        with (patch("qsv_local.batch.require_completed", side_effect=DownloadNotReady("客户端下载状态已变化")),
              patch("qsv_local.batch.fingerprint", wraps=fingerprint) as source_hash):
            result, _ = self.batch()
        self.assertEqual((result["completed"], result["pending_downloads"], result["failed"]), (0, 1, 0))
        source_hash.assert_called_once()
        self.assertEqual(list(self.output.rglob("*.mp4")), [])
        self.assertEqual(len(list(self.work.rglob("validated.partial.mp4"))), 1)

    def test_gpu_startup_error_does_not_fall_back_or_prepare_sources(self):
        """启用 GPU 后初始化失败时停止批次，不读取准备媒体或回退 CPU；无参数和返回值。"""
        self.config["decode_verification"] = {"cpu": True, "gpu": True}
        self.source()
        with (patch("qsv_local.batch.check_gpu", side_effect=RuntimeError("CUDA unavailable")),
              patch("qsv_local.batch.prepare") as preparer, redirect_stdout(self.console)):
            with self.assertRaisesRegex(RuntimeError, "CUDA unavailable"):
                run_batch(self.config)
        preparer.assert_not_called()

    def test_late_gpu_failure_preserves_cpu_commit_in_same_directory(self):
        """GPU 首项在 CPU 后项提交后失败，不能覆盖后项索引，也不能重新交给 CPU；无参数和返回值。"""
        self.config["decode_verification"] = {"cpu": True, "gpu": True}
        self.source("series/one.qsv")
        self.source("series/two.qsv")
        cpu_committed = Event()
        original_commit = Pipeline.commit
        original_verify = self.fake_verify
        attempted = []

        def commit(pipeline, job, result):
            """提交 pipeline/job/result 后通知 CPU 已完成，无返回值。"""
            original_commit(pipeline, job, result)
            if job.task.source.stem == "two":
                cpu_committed.set()

        def verify(config, report, work, notify, device, cancel_event):
            """用正式验证参数协调 GPU 晚失败、CPU 先成功；返回 CPU 证明或抛出 GPU 错误。"""
            attempted.append((work.name, device))
            notify("verifying", device=device)
            if work.name == "one.qsv":
                self.signal(cpu_committed)
                raise RuntimeError("GPU decode failed")
            return original_verify(config, report, work, notify, device, cancel_event)

        with patch.object(self, "fake_verify", side_effect=verify), patch.object(Pipeline, "commit", commit):
            result, _ = self.batch()
        self.assertEqual((result["completed"], result["failed"]), (1, 1), self.console.getvalue())
        index = read_json(self.target("series/two.qsv").parent / "video_info.json")
        self.assertEqual([item["path"] for item in index["files"]], [self.target("series/two.qsv").name])
        self.assertEqual(attempted, [("one.qsv", "gpu"), ("two.qsv", "cpu")])
        self.assertTrue(self.target("series/two.qsv").is_file())

    def test_index_write_failure_preserves_previous_success_and_removes_uncommitted_output(self):
        """第二个成品提交失败，仅撤销该成品，不损失先前成功记录；无参数和返回值。"""
        self.source("series/one.qsv")
        self.source("series/two.qsv")

        def fail_second(path, data):
            """在 path/data 将登记第二个成品时模拟错误；其他写入使用真实原子保存，无返回值。"""
            if len(data["files"]) == 2:
                raise OSError("fixture index failure")
            atomic_json(path, data)

        with patch("qsv_local.records.atomic_json", side_effect=fail_second):
            result, _ = self.batch()
        self.assertEqual((result["completed"], result["failed"]), (1, 1))
        index = read_json(self.target("series/one.qsv").parent / "video_info.json")
        self.assertEqual(len(index["files"]), 1)
        self.assertEqual(len(list(self.target("series/one.qsv").parent.glob("*.mp4"))), 1)

    def test_interrupt_cancels_other_device_and_rolls_back_uncommitted_publication(self):
        """发布后提交前中断，取消另一个验证设备并删除未登记成品，保留两个候选；无参数和返回值。"""
        self.config["decode_verification"] = {"cpu": True, "gpu": True}
        self.source("series/one.qsv")
        self.source("series/two.qsv")
        cpu_started, cpu_cancelled = Event(), Event()

        def verify(config, report, work, notify, device, cancel_event):
            """按正式验证参数让 CPU 等待取消，GPU 完成后触发提交中断；返回 GPU 证明或抛取消异常。"""
            notify("verifying", device=device)
            if device == "cpu":
                cpu_started.set()
                self.signal(cancel_event)
                cpu_cancelled.set()
                raise TaskCancelled("fixture cancelled")
            self.signal(cpu_started)
            return ProjectCase.fake_verify(self, config, report, work, notify, device, cancel_event)

        with (patch("qsv_local.batch.prepare", side_effect=self.fake_prepare),
              patch("qsv_local.batch.verify", side_effect=verify), patch("qsv_local.batch.check_gpu"),
              patch.object(Pipeline, "commit", side_effect=KeyboardInterrupt()),
              redirect_stdout(self.console), redirect_stderr(self.console)):
            with self.assertRaises(KeyboardInterrupt):
                run_batch(self.config)
        self.assertTrue(cpu_cancelled.is_set())
        self.assertEqual(list(self.output.rglob("*.mp4")), [])
        self.assertEqual(len(list(self.work.rglob("validated.partial.mp4"))), 2)
        index = read_json(self.target("series/one.qsv").parent / "video_info.json")
        self.assertEqual(index["files"], [])
        self.assertTrue(all(item["status"] == "interrupted" for item in index["conversion_results"]))
