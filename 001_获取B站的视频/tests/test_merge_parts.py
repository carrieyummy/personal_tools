"""离线验证分组拼接、失败隔离及实际媒体的顺序和重复片段；不读取真实下载目录。"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import download_bilibili as tool
import video_metadata as metadata
from tests import PROJECT_DIR


class MergeGroupTests(unittest.TestCase):
    """使用临时记录和模拟 FFmpeg 验证参数、批次输出及逐组错误处理。"""

    def setUp(self) -> None:
        """创建临时目录并模拟 FFmpeg 输出和媒体探测；无返回值。"""
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.settings = replace(tool.load_settings(PROJECT_DIR / "config.yaml"), output_dir=self.root)
        self.bvid = "BV1xx411c7mD"
        self.formats = [{"vcodec": "avc1", "acodec": "none", "width": 64, "height": 64}]
        self.ffmpeg = self.stack.enter_context(patch.object(tool.subprocess, "run", side_effect=self.concatenate))
        self.stack.enter_context(patch.object(metadata, "probe_media", return_value={"duration_seconds": 1.0, "errors": []}))

    def tearDown(self) -> None:
        """恢复模拟并删除所有临时媒体和记录；无返回值。"""
        self.stack.close()

    def concatenate(self, command: list[str], **options: object) -> subprocess.CompletedProcess:
        """按 command 的目标路径模拟生成非空文件；options 为 subprocess 选项，返回成功的进程结果。"""
        Path(command[-1]).write_bytes(b"merged-video")
        return subprocess.CompletedProcess(command, 0, "", "")

    def make_record(self, name: str = "default", count: int = 3) -> tool.VideoInfo:
        """在 name 测试子目录创建 count 个已完成分 P；返回已保存的统一记录，不依赖真实媒体。"""
        record = tool.open_video(self.root / name, self.bvid, "测试投稿", count)
        for part in range(1, count + 1):
            path = record.path.parent / f"{self.bvid}_p{part}" / f"片段{part}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"part-{part}".encode())
            item = record.entry(f"{self.bvid}_p{part}", part=part)
            item["formats"] = self.formats
            record.record_file(item, path)
        record.save()
        return record

    def test_cli_requires_merge_mode_and_accepts_blank(self) -> None:
        """验证范围参数仅用于手动拼接或全部分 P 下载，支持留空及普通表达式；无返回值。"""
        with redirect_stderr(io.StringIO()):
            for args in (["--merge-parts", "1-2"], ["--login-only", "--merge-parts", ""],
                         ["--refresh-metadata", "--merge-parts", "1,1"],
                         ["--all-parts", "--list-formats", "--merge-parts", "1,1"]):
                with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                    tool.parse_args(args)
                self.assertEqual(error.exception.code, 2)
        for expression in (None, "", "   ", "1-5,8,10-12;10-12"):
            args = ["--merge-video", self.bvid]
            if expression is not None:
                args.extend(["--merge-parts", expression])
            self.assertEqual(tool.parse_args(args).merge_parts, expression)
            args = ["--url", self.bvid, "--all-parts"]
            if expression is not None:
                args.extend(["--merge-parts", expression])
            self.assertEqual(tool.parse_args(args).merge_parts, expression)

    def test_parse_preserves_input_order_duplicates_and_whitespace(self) -> None:
        """验证范围展开只保留输入顺序，重复编号不去重，倒置及非法范围被拒绝；无返回值。"""
        self.assertEqual(tool.parse_merge_group(" 10 - 12 , 1 - 3 , 3 "), [10, 11, 12, 1, 2, 3, 3])
        self.assertEqual(tool.parse_merge_group("3,1,3"), [3, 1, 3])
        self.assertEqual(tool.parse_merge_group("1,1"), [1, 1])
        for expression in ("", " ", "1", "1-1", "1,", ",1", "0,1", "-1,2", "3-2", "1,x", "p1,p2", "1.5,2", "1,,2", "1，2"):
            with self.subTest(expression=expression), self.assertRaises(tool.ToolError):
                tool.parse_merge_group(expression)

    def test_each_group_failure_preserves_later_success(self) -> None:
        """验证语法、状态、文件、编码、重名和超长名称失败仅影响对应组；无返回值。"""
        cases = [
            ("", "表达式无效"), ("1,x", "表达式无效"), ("3-2", "起点"), ("0,1", "正整数"),
            ("1", "至少"), ("1,99", "尚未下载"), ("1,4", "尚未完成"),
            ("1,5", "编码或分辨率"), ("1,6", "文件不存在"), ("1,7", "文件不存在"),
            ("1,8", "缺少编码信息"), ("1,1", "已存在"), (",".join(["1", "3"] * 100), "长度"),
        ]
        for index, (expression, reason) in enumerate(cases):
            with self.subTest(expression=expression):
                record = self.make_record(f"case{index}", 8)
                items = {item["part"]: item for item in record.data["files"]}
                items[4]["status"] = "interrupted"
                items[5]["formats"] = [{**self.formats[0], "vcodec": "hevc"}]
                record.resolve_file(items[6]).unlink()
                record.resolve_file(items[7]).write_bytes(b"")
                items[8]["formats"] = []
                existing = record.path.parent / f"测试投稿_{self.bvid}_p1_p1.mp4"
                existing.write_bytes(b"do-not-overwrite")
                report = io.StringIO()
                with redirect_stdout(report):
                    result = tool.merge_groups(self.settings, "ffmpeg", record, expression + ";2,2")
                self.assertEqual(result, 2)
                self.assertIn("第 1 组", report.getvalue())
                self.assertIn(reason, report.getvalue())
                self.assertIn("第 2 组 [2,2] 成功", report.getvalue())
                self.assertIn("成功 1 组，失败 1 组", report.getvalue())
                self.assertEqual(existing.read_bytes(), b"do-not-overwrite")
                merged = [item for item in record.data["files"] if item["kind"] == "merged"]
                self.assertEqual([item["source_parts"] for item in merged], [[2, 2]])
                self.assertTrue(record.resolve_file(merged[0]).is_file())

    def test_ffmpeg_errors_do_not_stop_other_groups(self) -> None:
        """验证 FFmpeg 非零退出及启动异常不阻塞下一组，且失败组不留下正式输出；无返回值。"""
        for index, failure in enumerate((subprocess.CompletedProcess([], 1, "", "test failure"), OSError("test launch failure"))):
            with self.subTest(failure=failure):
                record = self.make_record(f"ffmpeg{index}")
                success = subprocess.CompletedProcess([], 0, "", "")

                def transfer(command: list[str], **options: object) -> subprocess.CompletedProcess:
                    """模拟 command 的首组失败、后续组成功；options 为进程选项，返回进程结果或抛出启动错误。"""
                    Path(command[-1]).write_bytes(b"temporary-output")
                    if transfer.first:
                        transfer.first = False
                        if isinstance(failure, OSError):
                            raise failure
                        return failure
                    return success

                transfer.first = True
                with patch.object(tool.subprocess, "run", side_effect=transfer), redirect_stdout(io.StringIO()):
                    self.assertEqual(tool.merge_groups(self.settings, "ffmpeg", record, "1,1;2,2"), 2)
                self.assertFalse((record.path.parent / f"测试投稿_{self.bvid}_p1_p1.mp4").exists())
                self.assertEqual([item["source_parts"] for item in record.data["files"] if item["kind"] == "merged"], [[2, 2]])
                self.assertEqual(list(record.path.parent.glob(".merge-*")), [])

    def test_run_blank_uses_full_merge_and_old_records_remain_readable(self) -> None:
        """验证未传入或空白范围保持完整拼接命名，新旧结果记录可共存且无需认证；无返回值。"""
        for index, expression in enumerate((None, "", "   ")):
            with self.subTest(expression=expression):
                record = self.make_record(f"blank{index}")
                legacy = record.entry("old:merged", "merged")
                record.save()
                arguments = ["--merge-video", self.bvid]
                if expression is not None:
                    arguments.extend(["--merge-parts", expression])
                settings = replace(self.settings, output_dir=record.output_dir)
                with patch.object(tool, "load_settings", return_value=settings), patch.object(tool, "resolve_ffmpeg", return_value="ffmpeg"), patch.object(tool, "authenticated_session") as login, redirect_stdout(io.StringIO()):
                    self.assertEqual(tool.run(tool.parse_args(arguments)), 0)
                login.assert_not_called()
                stored = tool.get_video_info(settings, self.bvid)
                output = next(item for item in stored.data["files"] if item["id"] == f"{self.bvid}:merged")
                self.assertEqual(output["file_name"], f"测试投稿_{self.bvid}.mp4")
                self.assertEqual(output["source_parts"], [1, 2, 3])
                self.assertEqual(stored.data["schema_version"], 1)
                self.assertEqual(next(item for item in stored.data["files"] if item["id"] == legacy["id"]), legacy)

    def test_full_merge_still_requires_all_parts(self) -> None:
        """验证全部拼接仍拒绝缺失或未完成分 P，显式重复选择可输出独立文件；无返回值。"""
        record = self.make_record()
        record.data["total_parts"] = 4
        with self.assertRaisesRegex(tool.ToolError, "分 P 不完整"):
            tool.merge_parts(self.settings, "ffmpeg", record)
        pending = record.entry(f"{self.bvid}_p4", part=4)
        pending["status"] = "failed"
        with self.assertRaisesRegex(tool.ToolError, "尚未完成"):
            tool.merge_parts(self.settings, "ffmpeg", record)
        self.ffmpeg.assert_not_called()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(tool.merge_groups(self.settings, "ffmpeg", record, "1-3;1,2,3;1-3"), 0)
        merged = [item for item in record.data["files"] if item["kind"] == "merged"]
        self.assertEqual([item["file_name"] for item in merged], [f"测试投稿_{self.bvid}_p1-3{suffix}.mp4" for suffix in ("", "_2", "_3")])
        self.assertEqual(len({item["id"] for item in merged}), 3)
        self.assertEqual([item["source_parts"] for item in merged], [[1, 2, 3]] * 3)

    def test_mixed_containers_output_mkv(self) -> None:
        """验证所选组包含 MKV 时输出 MKV，未选择的分 P 不改变该组容器；无返回值。"""
        record = self.make_record()
        item = record.data["files"][2]
        source = record.resolve_file(item)
        moved = source.with_suffix(".mkv")
        source.rename(moved)
        record.record_file(item, moved)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(tool.merge_groups(self.settings, "ffmpeg", record, "1,2;2,3"), 0)
        self.assertEqual([Path(item["file_name"]).suffix for item in record.data["files"] if item["kind"] == "merged"], [".mp4", ".mkv"])


class MergeMediaTests(unittest.TestCase):
    """使用真实 FFmpeg 短视频验收分组输出的帧顺序、重复项及持久化来源。"""

    def test_multiple_outputs_preserve_exact_frames_and_sources(self) -> None:
        """生成十二段不同画面的编码视频，验证示例、倒序组合及重复分组的实际帧哈希；无返回值。"""
        settings = tool.load_settings(PROJECT_DIR / "config.yaml")
        executable = tool.resolve_ffmpeg(settings, PROJECT_DIR)
        bvid = "BV1xx411c7mD"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(settings, output_dir=root / "downloads")
            samples = root / "samples"
            samples.mkdir()
            command = [executable, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=duration=12:size=64x64:rate=5",
                       "-c:v", "libx264", "-g", "5", "-sc_threshold", "0", "-f", "segment", "-segment_time", "1",
                       "-reset_timestamps", "1", "-segment_start_number", "1", str(samples / "part%d.mp4")]
            generated = subprocess.run(command, capture_output=True)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            record = tool.open_video(settings.output_dir, bvid, "测试'投稿", 15)
            inputs = {}
            for part in range(1, 13):
                path = record.path.parent / f"{bvid}_p{part}" / f"片段'{part}.mp4"
                path.parent.mkdir(parents=True, exist_ok=True)
                (samples / f"part{part}.mp4").rename(path)
                item = record.entry(f"{bvid}_p{part}", part=part)
                item["formats"] = [{"vcodec": "avc1", "acodec": "none", "width": 64, "height": 64}]
                record.record_file(item, path)
                inputs[part] = path
            record.entry(f"{bvid}_p13", part=13)["status"] = "interrupted"
            record.save()
            expression = "1-5,8,10-12;10-12;3,1,3;10-12,1-5;3,1,3"
            args = tool.parse_args(["--merge-video", bvid, "--merge-parts", expression])
            with patch.object(tool, "load_settings", return_value=settings), patch.object(tool, "authenticated_session") as login, redirect_stdout(io.StringIO()):
                self.assertEqual(tool.run(args), 0)
            login.assert_not_called()
            stored = tool.get_video_info(settings, bvid)
            inputs = {item["part"]: stored.resolve_file(item) for item in stored.data["files"] if item["kind"] == "part" and item["status"] == "completed"}
            outputs = [item for item in stored.data["files"] if item["kind"] == "merged"]
            expected = [[1, 2, 3, 4, 5, 8, 10, 11, 12], [10, 11, 12], [3, 1, 3], [10, 11, 12, 1, 2, 3, 4, 5], [3, 1, 3]]
            names = ["p1-5_p8_p10-12", "p10-12", "p3_p1_p3", "p10-12_p1-5", "p3_p1_p3_2"]
            self.assertEqual([item["source_parts"] for item in outputs], expected)
            self.assertEqual([item["file_name"] for item in outputs], [f"测试'投稿_{bvid}_{name}.mp4" for name in names])
            self.assertEqual(len({item["id"] for item in outputs}), 5)

            def frames(path: Path) -> list[str]:
                """读取 path 的实际解码帧并返回有序哈希列表，用于比较拼接前后的画面及顺序。"""
                result = subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return [line.rsplit(",", 1)[-1].strip() for line in result.stdout.splitlines() if line and not line.startswith("#")]

            input_frames = {part: frames(path) for part, path in inputs.items()}
            for item, sequence in zip(outputs, expected):
                self.assertEqual(frames(stored.resolve_file(item)), [frame for part in sequence for frame in input_frames[part]])
                self.assertEqual(item["processing"], "stream copy")
                self.assertEqual(item["status"], "completed")
            self.assertTrue(all(path.is_file() for path in inputs.values()))
            before = {item["id"]: stored.resolve_file(item).read_bytes() for item in outputs}
            with redirect_stdout(io.StringIO()):
                self.assertEqual(tool.merge_groups(settings, executable, stored, expression), 2)
            self.assertEqual({item["id"]: stored.resolve_file(item).read_bytes() for item in outputs}, before)
            self.assertEqual(list(record.path.parent.glob(".merge-*")), [])
