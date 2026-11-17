"""核对 FFmpeg 实际属性提取，避免把解码器名称当成编码档次。"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qsv_local.process import probe_media


class MediaProbeTests(unittest.TestCase):
    """用合成 FFmpeg 输入头检查编码、位深像素格式与纯音轨属性。"""

    def probe(self, video: str, require_video: bool = True) -> dict:
        """将合成 video 描述交给正式探测器，require_video 控制纯音轨模式；返回提取属性。"""
        header = "Duration: 00:00:30.00, start: 0.000000\n"
        if video:
            header += "Stream #0:0: Video: " + video + ", 3840x2160, 25 fps\n"
        header += "Stream #0:1: Audio: aac (LC), 48000 Hz, stereo, fltp\nStream mapping:\n"

        def process(command, log_path, config, **options):
            """为 command/config 指定的探测写入 log_path；options 为取消等执行选项，无返回值。"""
            log_path.write_text(header, encoding="utf-8")

        with tempfile.TemporaryDirectory() as directory, patch("qsv_local.process.run_process", side_effect=process):
            return probe_media({"ffmpeg_path": "synthetic"}, Path("synthetic.mp4"), Path(directory) / "probe.log", require_video=require_video)

    def test_av1_decoder_name_is_not_reported_as_profile(self):
        """FFmpeg 可在 profile 前输出 libaom-av1，实际档次仍应为 Main；无返回值。"""
        result = self.probe("av1 (libaom-av1) (Main) (av01 / 0x31307661), yuv420p(tv)")
        self.assertEqual(result["video_profile"], "Main")
        self.assertEqual(result["pixel_format"], "yuv420p")
        self.assertEqual(result["audio_layouts"], ["stereo"])

    def test_hevc_main10_keeps_actual_pixel_format(self):
        """相同分辨率下保留 Main 10 与 10 位像素格式，不能只报告 4K；无返回值。"""
        result = self.probe("hevc (Main 10) (hvc1 / 0x31637668), yuv420p10le(tv)")
        self.assertEqual(result["video_profile"], "Main 10")
        self.assertEqual(result["pixel_format"], "yuv420p10le")

    def test_audio_only_probe_does_not_require_video_properties(self):
        """独立音轨必须可校验时长与音频，无视频是合法状态；无返回值。"""
        result = self.probe("", require_video=False)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["duration_seconds"], 30)
        self.assertEqual(result["audio_codecs"], ["aac"])


if __name__ == "__main__":
    unittest.main()
