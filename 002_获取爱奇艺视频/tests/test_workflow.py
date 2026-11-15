"""验证候选媒体的完整验证门槛、子进程回收以及命令行退出码。"""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock, patch

import decode_qsv
from qsv_local.process import run_process
from qsv_local.storage import read_json
from qsv_local.workflow import convert
from tests.support import MEDIA, ProjectCase


class WorkflowTests(ProjectCase):
    """隔离 SDK 和 FFmpeg，仅验证单文件流程的验证证明与失败保留。"""

    def run_workflow(self, *, frames: int = 2, mutate_source: bool = False) -> tuple[dict, Path]:
        """运行合成流水线；frames 控制解码帧数，mutate_source 模拟验证期间换片；返回报告和工作路径。"""
        source, work = self.source(), self.work / "attempt"
        qsv = MagicMock()
        qsv.info = {"license_version": "5"}
        qsv.container_version = 2

        def extract(video, audio, buffer_size, progress):
            """为 video/audio 写入测试数据，buffer_size 为正式块长，progress 接收完成字节；无返回值。"""
            video.write_bytes(b"outer")
            audio.write_bytes(b"audio")
            progress(1, 1)

        def transport(input_path, output_path, progress, progress_bytes):
            """将测试 input_path 转到 output_path，progress/progress_bytes 为进度接口；返回固定结构统计。"""
            output_path.write_bytes(b"clear")
            progress(1, 1)
            return {"video_pes": 2, "stream_type": 0x24, "protection": "clear", "descriptor_styles": {}}

        def media_process(command, log_path, config, progress=None, progress_path=None, *, cancel_event=None):
            """模拟 command/log_path/config 对应媒体任务；progress/progress_path 为进度，cancel_event 为取消信号；无返回值。"""
            log_path.write_text("", encoding="utf-8")
            if "-progress" in command:
                values = f"frame={frames}\nout_time_us=1000000\ndup_frames=0\ndrop_frames=0\nprogress=end\n"
                progress_path.write_text(values, encoding="utf-8")
                progress({"frame": str(frames)})
                if mutate_source:
                    source.write_bytes(b"client-replaced-video")
            else:
                Path(command[-1]).write_bytes(b"candidate-video")

        qsv.extract.side_effect = extract
        with (patch("qsv_local.workflow.QsvFile", return_value=qsv),
              patch("qsv_local.workflow.NativeV5"),
              patch("qsv_local.workflow.TransportDecoder") as decoder,
              patch("qsv_local.workflow.run_process", side_effect=media_process),
              patch("qsv_local.workflow.probe_media", return_value=MEDIA)):
            decoder.return_value.convert.side_effect = transport
            return convert(self.config, source, work, MagicMock()), work

    def test_verified_candidate_is_kept_until_directory_commit(self):
        """验证通过后保留候选媒体，交由批次提交后清理；无参数或返回值。"""
        report, work = self.run_workflow()
        self.assertEqual(report["status"], "verified")
        self.assertTrue((work / "validated.partial.mp4").exists())
        self.assertTrue(report["source_unchanged"])

    def test_frame_mismatch_is_not_published(self):
        """帧数不匹配时失败并保存报告及候选文件；无参数或返回值。"""
        with self.assertRaisesRegex(RuntimeError, "帧数"):
            self.run_workflow(frames=1)
        report = read_json(self.work / "attempt/report.json")
        self.assertEqual(report["status"], "failed")
        self.assertTrue((self.work / "attempt/validated.partial.mp4").exists())

    def test_source_replacement_during_conversion_is_rejected(self):
        """客户端在转换期间换片时拒绝生成成功证明；无参数或返回值。"""
        with self.assertRaisesRegex(RuntimeError, "源文件发生变化"):
            self.run_workflow(mutate_source=True)
        self.assertFalse(read_json(self.work / "attempt/report.json")["source_unchanged"])


class ProcessTests(ProjectCase):
    """验证实际子进程失败传播和取消时的强制回收。"""

    def test_nonzero_child_exit_preserves_stderr(self):
        """子进程非零退出时记录错误输出并报告失败；无参数或返回值。"""
        self.work.mkdir()
        log = self.work / "process.log"
        with self.assertRaisesRegex(RuntimeError, "退出码 7"):
            run_process([sys.executable, "-c", "import sys; sys.stderr.write('fixture-error'); sys.exit(7)"],
                        log, self.config)
        self.assertIn("fixture-error", log.read_text())

    def test_interrupt_terminates_then_kills_stalled_process(self):
        """中断先终止进程，超过等待时限后强制回收；无参数或返回值。"""
        self.work.mkdir()
        child = MagicMock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("fixture", 1), 0]
        with (patch("qsv_local.process.subprocess.Popen", return_value=child),
              patch("qsv_local.process.time.sleep", side_effect=KeyboardInterrupt())):
            with self.assertRaises(KeyboardInterrupt):
                run_process(["fixture"], self.work / "process.log", self.config)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)


class CliTests(ProjectCase):
    """验证入口保持配置驱动及标准退出码。"""

    def test_batch_failure_sets_nonzero_exit_code(self):
        """有单项失败时入口返回 1，全部复用返回 0；无参数或返回值。"""
        with (patch.object(sys, "argv", ["decode_qsv.py", "--config", str(self.config_path)]),
              redirect_stdout(self.console)):
            for failures, expected in ((0, 0), (1, 1)):
                with patch("decode_qsv.run_batch", return_value={"failed": failures}):
                    self.assertEqual(decode_qsv.main(), expected)

    def test_keyboard_interrupt_sets_130_exit_code(self):
        """批次中断时入口返回 130；无参数或返回值。"""
        with (patch.object(sys, "argv", ["decode_qsv.py", "--config", str(self.config_path)]),
              patch("decode_qsv.run_batch", side_effect=KeyboardInterrupt()),
              redirect_stderr(self.console)):
            self.assertEqual(decode_qsv.main(), 130)
