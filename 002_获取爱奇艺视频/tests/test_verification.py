"""验证共享解码证明、可取消子进程及发布重命名前的取消处理。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
from threading import Event
import time
from unittest.mock import patch

from qsv_local.process import run_process
from qsv_local.runtime import TaskCancelled
from qsv_local.storage import fingerprint, publish
from qsv_local.verification import decode_command, validate_progress
from tests.support import ProjectCase


class VerificationTests(ProjectCase):
    """验证设备选择和所有设备共用的帧数判定，不依赖 GPU 或真实媒体。"""

    def test_shared_validation_rejects_incomplete_mismatched_duplicate_and_dropped_frames(self):
        """完整证明可通过，结束标记、帧数及重复/丢帧异常均拒绝；无参数和返回值。"""
        progress = {"progress": "end", "frame": "4", "out_time_us": "1000000", "dup_frames": "0", "drop_frames": "0"}
        self.assertEqual(validate_progress(progress, 4)["frames"], 4)
        for fields in ({"progress": "continue"}, {"frame": "3"}, {"frame": "0"},
                       {"dup_frames": "1"}, {"drop_frames": "1"}):
            with self.subTest(fields=fields), self.assertRaises(RuntimeError):
                validate_progress({**progress, **fields}, 4)

    def test_gpu_command_keeps_cuda_frames_and_maps_all_audio(self):
        """正式和性能比较共用命令；GPU 保留设备帧，CPU 不初始化 GPU，全部音轨均被映射；无参数和返回值。"""
        cpu = decode_command(self.config, self.input / "video.mp4", self.work / "cpu.log", "cpu")
        gpu = decode_command(self.config, self.input / "video.mp4", self.work / "gpu.log", "gpu")
        self.assertNotIn("-hwaccel", cpu)
        self.assertEqual(gpu[gpu.index("-hwaccel") + 1], "cuda")
        self.assertEqual(gpu[gpu.index("-hwaccel_output_format") + 1], "cuda")
        self.assertIn("0:a", gpu)
        self.assertIn("-xerror", gpu)
        self.assertEqual(gpu[0], str(self.config["ffmpeg_path"]))

    def test_cancel_signal_terminates_running_media_child(self):
        """真实子进程已启动后取消，线程收到 TaskCancelled 且子进程已回收；无参数和返回值。"""
        self.work.mkdir()
        marker = self.work / "ready"
        cancel, children = Event(), []
        original_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            """转发 Popen 的 args/kwargs 并记录本测试子进程，返回真实句柄。"""
            child = original_popen(*args, **kwargs)
            children.append(child)
            return child

        command = [sys.executable, "-c", "from pathlib import Path; import sys,time; Path(sys.argv[1]).touch(); time.sleep(float(sys.argv[2]))",
                   str(marker), str(self.config["process_timeout_seconds"])]
        with patch("qsv_local.process.subprocess.Popen", side_effect=spawn), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_process, command, self.work / "child.log", self.config, cancel_event=cancel)
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(self.config["runtime"]["cancel_poll_interval_seconds"])
                self.assertTrue(marker.exists(), "测试子进程未按时启动")
                cancel.set()
                with self.assertRaises(TaskCancelled):
                    future.result(timeout=10)
                self.assertIsNotNone(children[0].poll())
            finally:
                cancel.set()

    def test_media_timeout_terminates_child(self):
        """媒体进程超过配置时限时抛出 TimeoutError 并回收子进程；无参数和返回值。"""
        self.work.mkdir()
        with (patch("qsv_local.process.subprocess.Popen") as popen,
              patch("qsv_local.process.time.monotonic", side_effect=[0, self.config["process_timeout_seconds"] + 1])):
            child = popen.return_value
            child.poll.return_value = None
            with self.assertRaises(TimeoutError):
                run_process(["fixture"], self.work / "timeout.log", self.config)
            child.terminate.assert_called_once()
            child.wait.assert_called_once()

    def test_cancel_before_rename_removes_output_temporary_file(self):
        """复制校验结束、最终重命名前收到取消时不发布文件，且清除输出临时副本；无参数和返回值。"""
        source = self.source()
        digest = fingerprint(source, self.config["copy_buffer_bytes"])["sha256"]
        cancel = Event()
        original = fingerprint

        def cancel_after_hash(path, buffer_bytes, cached=None, **kwargs):
            """按原 path/buffer_bytes/cached/kwargs 校验复制副本后取消；返回真实指纹。"""
            result = original(path, buffer_bytes, cached, **kwargs)
            cancel.set()
            return result

        with patch("qsv_local.storage.fingerprint", side_effect=cancel_after_hash):
            with self.assertRaises(TaskCancelled):
                publish(source, self.output / "candidate.mp4", digest, self.config["copy_buffer_bytes"], cancel_event=cancel)
        self.assertEqual(list(self.output.iterdir()), [])
