"""离线验证分组拼接、失败隔离及实际媒体的顺序和重复片段；不读取真实下载目录。"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import yaml

from bilibili_downloader import application, auth, errors, library, media, metadata, models
from bilibili_downloader import merge as merge_service
from bilibili_downloader.merged_records import MergedVideoInfo
from bilibili_downloader import config as configuration
import download_bilibili as cli_entry
from bilibili_downloader import runtime
from tests import CONFIG_PATH


class MergeGroupTests(unittest.TestCase):
    """使用临时记录和模拟 FFmpeg 验证参数、批次输出及逐组错误处理。"""

    def setUp(self) -> None:
        """创建临时目录并模拟 FFmpeg 输出和媒体探测；无返回值。"""
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.settings = replace(configuration.load_settings(CONFIG_PATH), output_dir=self.root, overwrite_merged=True)
        self.bvid = "BV1xx411c7mD"
        self.formats = [{"vcodec": "avc1", "acodec": "none", "width": 64, "height": 64}]
        self.ffmpeg = self.stack.enter_context(patch.object(runtime, "run_process", side_effect=self.concatenate))
        self.stack.enter_context(patch.object(metadata, "probe_media", return_value={"duration_seconds": 1.0, "errors": []}))

    def tearDown(self) -> None:
        """恢复模拟并删除所有临时媒体和记录；无返回值。"""
        self.stack.close()

    def concatenate(self, command: list[str], **options: object) -> subprocess.CompletedProcess:
        """按 command 的目标路径模拟生成非空文件；options 为 subprocess 选项，返回成功的进程结果。"""
        Path(command[-1]).write_bytes(b"merged-video")
        return subprocess.CompletedProcess(command, 0, "", "")

    def replacement_media(self, command: list[str], **options: object) -> subprocess.CompletedProcess:
        """为 command 写入与旧成品不同的媒体字节，options 为进程选项，返回成功结果用于验证回滚。"""
        Path(command[-1]).write_bytes(b'different-new-merged-content')
        return subprocess.CompletedProcess(command, 0, '', '')

    def make_record(self, name: str = "default", count: int = 3) -> metadata.VideoInfo:
        """在 name 测试子目录创建 count 个已完成分 P；返回已保存的统一记录，不依赖真实媒体。"""
        record = metadata.open_video(self.root / name, self.bvid, "测试投稿", count)
        record.data['schema_version'] = 4
        for part in range(1, count + 1):
            path = record.path.parent / f"{self.bvid}_p{part}" / f"片段{part}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"part-{part}".encode())
            item = record.entry(f"{self.bvid}_p{part}", part=part)
            item["formats"] = self.formats
            record.record_file(item, path)
        record.save()
        return record

    def merged_index(self, record: metadata.VideoInfo) -> MergedVideoInfo:
        """读取 record 所在投稿的默认清晰度合并索引，返回独立记录对象。"""
        return MergedVideoInfo(record.output_dir, record.quality_directory(models.LEGACY_QUALITY, role='merged'))

    def test_cli_requires_merge_mode_and_accepts_blank(self) -> None:
        """验证范围参数的使用限制、模式默认值及显式选项；无返回值。"""
        with redirect_stderr(io.StringIO()):
            for args in (["--merge-parts", "1-2"], ["--login-only", "--merge-parts", ""],
                         ["--refresh-metadata", "--merge-parts", "1,1"],
                         ["--all-parts", "--list-formats", "--merge-parts", "1,1"],
                         ["--auto-merge-parts"]):
                with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                    cli_entry.parse_args(args)
                self.assertEqual(error.exception.code, 2)
        for expression in (None, "-1", "0", "", "   ", "1-5,8,10-12;10-12"):
            args = ["--merge-video", self.bvid]
            if expression is not None:
                args.extend(["--merge-parts", expression])
            self.assertEqual(cli_entry.parse_args(args).merge_parts, "0" if expression is None else expression)
            args = ["--url", self.bvid, "--all-parts"]
            if expression is not None:
                args.extend(["--merge-parts", expression])
            self.assertEqual(cli_entry.parse_args(args).merge_parts, "-1" if expression is None else expression)

    def test_parse_preserves_input_order_duplicates_and_whitespace(self) -> None:
        """验证范围展开只保留输入顺序，重复编号不去重，倒置及非法范围被拒绝；无返回值。"""
        self.assertEqual(merge_service.parse_merge_group(" 10 - 12 , 1 - 3 , 3 "), [10, 11, 12, 1, 2, 3, 3])
        self.assertEqual(merge_service.parse_merge_group("3,1,3"), [3, 1, 3])
        self.assertEqual(merge_service.parse_merge_group("1,1"), [1, 1])
        for expression in ("", " ", "1", "1-1", "1,", ",1", "0,1", "-1,2", "3-2", "1,x", "p1,p2", "1.5,2", "1,,2", "1，2"):
            with self.subTest(expression=expression), self.assertRaises(errors.ToolError):
                merge_service.parse_merge_group(expression)

    def test_each_group_failure_preserves_later_success(self) -> None:
        """验证语法、状态、文件、编码和超长名称失败仅影响对应组；无返回值。"""
        cases = [
            ("", "表达式无效"), ("1,x", "表达式无效"), ("3-2", "起点"), ("0,1", "正整数"),
            ("1", "至少"), ("1,99", "尚未下载"), ("1,4", "尚未完成"),
            ("1,5", "编码或分辨率"), ("1,6", "文件不存在"), ("1,7", "文件不存在"),
            ("1,8", "缺少编码信息"), (",".join(["1", "3"] * 100), "长度"),
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
                folder = record.quality_directory(models.LEGACY_QUALITY, role='merged')
                folder.mkdir(exist_ok=True)
                existing = folder / f"测试投稿_{self.bvid}_p1_p1.mp4"
                existing.write_bytes(b"do-not-overwrite")
                report = io.StringIO()
                with redirect_stdout(report):
                    result = merge_service.merge_groups(self.settings, "ffmpeg", record, expression + ";2,2")
                self.assertEqual(result, 2)
                self.assertIn("第 1 组", report.getvalue())
                self.assertIn(reason, report.getvalue())
                self.assertIn("第 2 组 [2,2] 成功", report.getvalue())
                self.assertIn("成功 1 组，失败 1 组", report.getvalue())
                self.assertEqual(existing.read_bytes(), b"do-not-overwrite")
                index = self.merged_index(record)
                merged = index.data['files']
                self.assertEqual([item["source_parts"] for item in merged], [[2, 2]])
                self.assertTrue(index.resolve_file(merged[0]).is_file())
                self.assertTrue(all(item['kind'] != 'merged' for item in record.data['files']))
                results = MergedVideoInfo.for_results(record).data['merge_results']
                self.assertEqual([item['status'] for item in results], ['failed', 'completed'])
                self.assertEqual(results[0]['source_expression'], expression)
                self.assertIn(reason, results[0]['errors'][0])
                self.assertIsNone(results[0]['file_name'])
                self.assertTrue(results[0]['started'])
                self.assertTrue(results[0]['finished'])

    def test_full_merge_validation_failure_is_saved_without_fake_media(self) -> None:
        """验证全量合并在 FFmpeg 前失败也写入结果，索引不把失败请求登记成真实成品；无返回值。"""
        record = self.make_record()
        record.data['files'][1]['status'] = 'failed'
        record.save()
        with self.assertRaises(errors.ToolError):
            merge_service.merge_parts(self.settings, 'ffmpeg', record)
        result_index = MergedVideoInfo.for_results(record)
        self.assertEqual(result_index.data['files'], [])
        self.assertEqual(result_index.data['merge_results'][0]['status'], 'failed')
        self.assertEqual(result_index.data['merge_results'][0]['source_parts'], [1, 2, 3])
        self.assertIn('尚未完成', result_index.data['merge_results'][0]['errors'][0])
        self.ffmpeg.assert_not_called()

    def test_result_import_is_idempotent_and_keeps_failed_then_successful_attempts(self) -> None:
        """验证同一历史结果重复导入不重复，新一次成功保留先前失败及重试顺序；无返回值。"""
        record = self.make_record()
        for _ in range(2):
            merge_service.record_merge_result(record, status='failed', source_parts=[1, 2], expression='1-2',
                                              error='历史失败', result_id='historical-result')
        merge_service.merge_parts(self.settings, 'ffmpeg', record, [1, 2])
        results = MergedVideoInfo.for_results(record).data['merge_results']
        self.assertEqual([item['status'] for item in results], ['failed', 'completed'])
        self.assertEqual(results[0]['errors'], ['历史失败'])

    def test_full_merge_overwrites_and_refreshes_one_index_entry(self) -> None:
        """验证全量合并重跑替换旧成品，刷新大小和媒体属性且不重复登记；源分 P 保留，无返回值。"""
        record = self.make_record()
        destination = merge_service.merge_parts(self.settings, 'ffmpeg', record)
        sources = {record.resolve_file(item): record.resolve_file(item).read_bytes() for item in record.data['files']}

        def new_media(command: list[str], **options: object) -> subprocess.CompletedProcess:
            """按 command 输出新媒体；options 为进程选项，返回成功结果。"""
            Path(command[-1]).write_bytes(b'updated-merged-video-content')
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch.object(runtime, 'run_process', side_effect=new_media), patch.object(metadata, 'probe_media', return_value={'duration_seconds': 9, 'errors': []}):
            self.assertEqual(merge_service.merge_parts(self.settings, 'ffmpeg', record), destination)
        self.assertEqual(destination.read_bytes(), b'updated-merged-video-content')
        entries = self.merged_index(record).data['files']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['source_parts'], [1, 2, 3])
        self.assertEqual(entries[0]['size_bytes'], destination.stat().st_size)
        self.assertEqual(entries[0]['duration_seconds'], 9)
        self.assertEqual(entries[0]['status'], 'completed')
        self.assertTrue(all(path.read_bytes() == content for path, content in sources.items()))
        self.assertEqual(list(record.output_dir.rglob('.merge-*')), [])

    def test_overwrite_configuration_requires_yaml_boolean(self) -> None:
        """验证覆盖开关从 YAML 读取 true/false，拒绝数字、字符串和缺失字段；无返回值。"""
        original = yaml.safe_load(CONFIG_PATH.read_text(encoding='utf-8'))
        path = self.root / 'config.yaml'
        for expected in (True, False):
            with self.subTest(value=expected):
                original['merge']['overwrite_existing'] = expected
                path.write_text(yaml.safe_dump(original, allow_unicode=True), encoding='utf-8')
                self.assertIs(configuration.load_settings(path).overwrite_merged, expected)
        for value in (0, 1, None, 'false', [], {}):
            with self.subTest(value=value):
                original['merge']['overwrite_existing'] = value
                path.write_text(yaml.safe_dump(original, allow_unicode=True), encoding='utf-8')
                with self.assertRaisesRegex(errors.ToolError, 'merge.overwrite_existing'):
                    configuration.load_settings(path)
        del original['merge']['overwrite_existing']
        path.write_text(yaml.safe_dump(original, allow_unicode=True), encoding='utf-8')
        with self.assertRaises(errors.ToolError):
            configuration.load_settings(path)

    def test_overwrite_disabled_reuses_existing_media_without_changing_index(self) -> None:
        """验证关闭覆盖时保留成品及索引，不调用 FFmpeg 或媒体探测，日志显示复用且成功返回；无返回值。"""
        record = self.make_record()
        destination = merge_service.merge_parts(self.settings, 'ffmpeg', record)
        before = destination.read_bytes()
        index_path = self.merged_index(record).path
        index_bytes = index_path.read_bytes()
        modified = destination.stat().st_mtime_ns
        activity = MagicMock()
        self.ffmpeg.reset_mock()
        with patch.object(metadata, 'probe_media') as probe:
            actual = merge_service.merge_parts(replace(self.settings, overwrite_merged=False), 'ffmpeg', record, activity=activity)
        self.assertEqual(actual, destination)
        self.ffmpeg.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(destination.stat().st_mtime_ns, modified)
        self.assertEqual(index_path.read_bytes(), index_bytes)
        self.assertEqual(activity.update.call_args.args, ('reused',))
        self.assertEqual(activity.update.call_args.kwargs['path'], str(destination))

    def test_overwrite_disabled_reuses_duplicate_groups_and_creates_missing_group(self) -> None:
        """验证关闭覆盖时 _2 重复组正确复用，缺失组仍生成，其他组索引条目不变；无返回值。"""
        record = self.make_record()
        self.assertEqual(merge_service.merge_groups(self.settings, 'ffmpeg', record, '3,1,3;3,1,3'), 0)
        previous = self.merged_index(record)
        old_entries = [dict(entry) for entry in previous.data['files']]
        files = [previous.resolve_file(entry) for entry in old_entries]
        contents = {path: path.read_bytes() for path in files}
        self.ffmpeg.reset_mock()
        settings = replace(self.settings, overwrite_merged=False)
        self.assertEqual(merge_service.merge_groups(settings, 'ffmpeg', record, '3,1,3;3,1,3;2,2'), 0)
        self.assertEqual(self.ffmpeg.call_count, 1)
        self.assertTrue(all(path.read_bytes() == content for path, content in contents.items()))
        entries = self.merged_index(record).data['files']
        self.assertEqual(entries[:2], old_entries)
        self.assertEqual(entries[-1]['source_parts'], [2, 2])

    def test_overwrite_disabled_still_generates_missing_or_empty_target(self) -> None:
        """验证开关关闭也会生成不存在或为空的目标，不把空文件误判为已完成；无返回值。"""
        for empty in (False, True):
            with self.subTest(empty=empty):
                record = self.make_record(f'empty{empty}')
                if empty:
                    destination = merge_service.merge_parts(self.settings, 'ffmpeg', record)
                    destination.write_bytes(b'')
                self.ffmpeg.reset_mock()
                destination = merge_service.merge_parts(replace(self.settings, overwrite_merged=False), 'ffmpeg', record)
                self.assertEqual(self.ffmpeg.call_count, 1)
                self.assertEqual(destination.read_bytes(), b'merged-video')
                self.assertEqual(len(self.merged_index(record).data['files']), 1)

    def test_group_rerun_overwrites_each_occurrence_without_duplicate_entries(self) -> None:
        """验证分组合并及同批重复序列的 _2 输出再次运行时均覆盖，对应索引不增加重复条目；无返回值。"""
        record = self.make_record()
        expression = '3,1,3;1,1;3,1,3'
        self.assertEqual(merge_service.merge_groups(self.settings, 'ffmpeg', record, expression), 0)
        index = self.merged_index(record)
        files = [index.resolve_file(entry) for entry in index.data['files']]
        for path in files:
            path.write_bytes(b'old-content')
        self.assertEqual(merge_service.merge_groups(self.settings, 'ffmpeg', record, expression), 0)
        self.assertTrue(all(path.read_bytes() == b'merged-video' for path in files))
        entries = self.merged_index(record).data['files']
        self.assertEqual([entry['source_parts'] for entry in entries], [[3, 1, 3], [1, 1], [3, 1, 3]])
        self.assertEqual(len({entry['id'] for entry in entries}), 3)
        self.assertTrue(files[-1].stem.endswith('_2'))

    def test_generation_failure_or_cancel_keeps_existing_result_and_index(self) -> None:
        """验证重新合并时 FFmpeg 非零退出、启动失败或取消均不触碰旧成品及索引；无返回值。"""
        failures = [subprocess.CompletedProcess([], 1, '', '模拟失败'), OSError('启动失败'), errors.DownloadCancelled('取消')]
        for number, failure in enumerate(failures):
            with self.subTest(failure=failure):
                self.ffmpeg.side_effect = self.concatenate
                record = self.make_record(f'keep{number}')
                destination = merge_service.merge_parts(self.settings, 'ffmpeg', record)
                content, index_bytes = destination.read_bytes(), self.merged_index(record).path.read_bytes()
                if isinstance(failure, BaseException):
                    self.ffmpeg.side_effect = failure
                    expected = type(failure)
                else:
                    self.ffmpeg.side_effect = None
                    self.ffmpeg.return_value = failure
                    expected = errors.ToolError
                with self.assertRaises(expected):
                    merge_service.merge_parts(self.settings, 'ffmpeg', record)
                self.assertEqual(destination.read_bytes(), content)
                self.assertEqual(self.merged_index(record).path.read_bytes(), index_bytes)
                self.assertEqual(list(record.output_dir.rglob('.merge-*')), [])
                result = MergedVideoInfo.for_results(record).data['merge_results'][-1]
                self.assertEqual(result['status'], 'interrupted' if isinstance(failure, KeyboardInterrupt) else 'failed')
                self.assertTrue(result['errors'])

    def test_index_write_failure_restores_existing_media_and_index(self) -> None:
        """验证新索引已写出后报错时，旧成品和原索引仍完整回滚；无返回值。"""
        record = self.make_record()
        destination = merge_service.merge_parts(self.settings, 'ffmpeg', record)
        previous = destination.read_bytes()
        index_path = self.merged_index(record).path
        index_bytes = index_path.read_bytes()
        save = MergedVideoInfo.save

        def save_then_fail(index: MergedVideoInfo) -> None:
            """先真实写入 index 再模拟磁盘错误，验证事务覆盖已有索引的回滚；无返回值。"""
            save(index)
            raise OSError('写入后模拟失败')

        with patch.object(runtime, 'run_process', side_effect=self.replacement_media), patch.object(MergedVideoInfo, 'save', save_then_fail), self.assertRaises(OSError):
            merge_service.merge_parts(self.settings, 'ffmpeg', record)
        self.assertEqual(destination.read_bytes(), previous)
        self.assertEqual(index_path.read_bytes(), index_bytes)
        self.assertEqual(list(record.output_dir.rglob('.merge-*')), [])

    def test_cancel_after_replacement_restores_existing_media_and_index(self) -> None:
        """验证旧文件已暂存、新文件已就位后媒体探测被取消，仍恢复旧成品和索引；无返回值。"""
        record = self.make_record()
        destination = merge_service.merge_parts(self.settings, 'ffmpeg', record)
        previous = destination.read_bytes()
        index_path = self.merged_index(record).path
        index_bytes = index_path.read_bytes()
        with patch.object(runtime, 'run_process', side_effect=self.replacement_media), patch.object(metadata, 'probe_media', side_effect=errors.DownloadCancelled('取消探测')), self.assertRaises(errors.DownloadCancelled):
            merge_service.merge_parts(self.settings, 'ffmpeg', record)
        self.assertEqual(destination.read_bytes(), previous)
        self.assertEqual(index_path.read_bytes(), index_bytes)
        self.assertEqual(list(record.output_dir.rglob('.merge-*')), [])

    def test_target_replace_error_keeps_old_file_and_cleans_placeholder(self) -> None:
        """验证目标文件被占用、无法暂存时保留旧文件和索引，并清理空备份；无返回值。"""
        record = self.make_record()
        destination = merge_service.merge_parts(self.settings, 'ffmpeg', record)
        previous = destination.read_bytes()
        index_path = self.merged_index(record).path
        index_bytes = index_path.read_bytes()
        original = Path.replace

        def replace_file(source: Path, target: Path) -> Path:
            """仅模拟 source 旧成品转入 target 备份时被拒绝，其他路径真实替换并返回目标 Path。"""
            if source == destination and target.name.startswith('.merge-backup-'):
                raise PermissionError('模拟目标被占用')
            return original(source, target)

        with patch.object(Path, 'replace', replace_file), self.assertRaises(PermissionError):
            merge_service.merge_parts(self.settings, 'ffmpeg', record)
        self.assertEqual(destination.read_bytes(), previous)
        self.assertEqual(index_path.read_bytes(), index_bytes)
        self.assertEqual(list(record.output_dir.rglob('.merge-*')), [])

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
                with patch.object(runtime, "run_process", side_effect=transfer), redirect_stdout(io.StringIO()):
                    self.assertEqual(merge_service.merge_groups(self.settings, "ffmpeg", record, "1,1;2,2"), 2)
                self.assertFalse((record.path.parent / f"测试投稿_{self.bvid}_p1_p1.mp4").exists())
                self.assertEqual([item["source_parts"] for item in self.merged_index(record).data['files']], [[2, 2]])
                self.assertEqual(list(record.path.parent.glob(".merge-*")), [])

    def test_run_blank_uses_full_merge_and_old_records_remain_readable(self) -> None:
        """验证未传入或空白范围保持完整拼接命名，新旧结果记录可共存且无需认证；无返回值。"""
        for index, expression in enumerate((None, "0", "", "   ")):
            with self.subTest(expression=expression):
                record = self.make_record(f"blank{index}")
                legacy = record.entry("old:merged", "merged")
                record.save()
                arguments = ["--merge-video", self.bvid]
                if expression is not None:
                    arguments.extend(["--merge-parts", expression])
                settings = replace(self.settings, output_dir=record.output_dir)
                with patch.object(configuration, "load_settings", return_value=settings), patch.object(media, "resolve_ffmpeg", return_value="ffmpeg"), patch.object(auth, "authenticated_session") as login, redirect_stdout(io.StringIO()):
                    self.assertEqual(application.run(cli_entry.parse_args(arguments)), 0)
                login.assert_not_called()
                stored = library.get_video_info(settings, self.bvid)
                merged_files = self.merged_index(stored).data['files']
                output = next(item for item in merged_files if item["id"] == f"{self.bvid}:merged")
                quality_suffix = '_' + stored.quality_suffix(models.LEGACY_QUALITY) if settings.mix_quality else ''
                self.assertEqual(output["file_name"], f"测试投稿_{self.bvid}{quality_suffix}.mp4")
                self.assertEqual(output["source_parts"], [1, 2, 3])
                self.assertEqual(stored.data["schema_version"], 8)
                migrated = next(item for item in merged_files if item['id'] == self.bvid + ':' + legacy['id'])
                self.assertIsNone(migrated['source_parts'])
                self.assertTrue(all(item['kind'] != 'merged' for item in stored.data['files']))

    def test_run_disabled_merge_skips_all_work(self) -> None:
        """验证离线模式选择 -1 时直接结束且不加载配置或执行拼接；无返回值。"""
        args = cli_entry.parse_args(["--merge-video", self.bvid, "--merge-parts", "-1"])
        with patch.object(configuration, "load_settings") as load, patch.object(merge_service, "merge_parts") as merge, patch.object(merge_service, "merge_groups") as groups:
            self.assertEqual(application.run(args), 0)
        load.assert_not_called()
        merge.assert_not_called()
        groups.assert_not_called()

    def test_full_merge_still_requires_all_parts(self) -> None:
        """验证全部拼接仍拒绝缺失或未完成分 P，显式重复选择可输出独立文件；无返回值。"""
        record = self.make_record()
        record.data["total_parts"] = 4
        with self.assertRaisesRegex(errors.ToolError, "分 P 不完整"):
            merge_service.merge_parts(self.settings, "ffmpeg", record)
        pending = record.entry(f"{self.bvid}_p4", part=4)
        pending["status"] = "failed"
        with self.assertRaisesRegex(errors.ToolError, "尚未完成"):
            merge_service.merge_parts(self.settings, "ffmpeg", record)
        self.ffmpeg.assert_not_called()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(merge_service.merge_groups(self.settings, "ffmpeg", record, "1-3;1,2,3;1-3"), 0)
        merged = self.merged_index(record).data['files']
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
        record.save()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(merge_service.merge_groups(self.settings, "ffmpeg", record, "1,2;2,3"), 0)
        self.assertEqual([Path(item["file_name"]).suffix for item in self.merged_index(record).data['files']], [".mp4", ".mkv"])


class MergeMediaTests(unittest.TestCase):
    """使用真实 FFmpeg 短视频验收分组输出的帧顺序、重复项及持久化来源。"""

    def test_multiple_outputs_preserve_exact_frames_and_sources(self) -> None:
        """生成十二段不同画面的编码视频，验证示例、倒序组合及重复分组的实际帧哈希；无返回值。"""
        settings = configuration.load_settings(CONFIG_PATH)
        executable = media.resolve_ffmpeg(settings, CONFIG_PATH.parent)
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
            record = metadata.open_video(settings.output_dir, bvid, "测试'投稿", 15)
            record.data['schema_version'] = 4
            inputs = {}
            for part in range(1, 13):
                path = record.path.parent / f"{bvid}_p{part}" / f"片段'{part}.mp4"
                path.parent.mkdir(parents=True, exist_ok=True)
                (samples / f"part{part}.mp4").rename(path)
                item = record.entry(f"{bvid}_p{part}", part=part)
                item["formats"] = [{"vcodec": "avc1", "acodec": "none", "width": 64, "height": 64}]
                item['duration_seconds'] = 1.0
                record.record_file(item, path)
                inputs[part] = path
            record.entry(f"{bvid}_p13", part=13)["status"] = "interrupted"
            record.save()
            expression = "1-5,8,10-12;10-12;3,1,3;10-12,1-5;3,1,3"
            args = cli_entry.parse_args(["--merge-video", bvid, "--merge-parts", expression])
            with patch.object(configuration, "load_settings", return_value=settings), patch.object(auth, "authenticated_session") as login, redirect_stdout(io.StringIO()):
                self.assertEqual(application.run(args), 0)
            login.assert_not_called()
            log = metadata.read_json(settings.output_dir / settings.log_state_filename)
            activities = log['runs'][-1]['entries']
            self.assertEqual(len(activities), 5)
            self.assertTrue(all(activity['progress'] == 100 for activity in activities))
            self.assertTrue(all(activity['average_speed_bps'] > 0 for activity in activities))
            self.assertTrue(all(activity['metric_seconds'] > 0 for activity in activities))
            stored = library.get_video_info(settings, bvid)
            inputs = {item["part"]: stored.resolve_file(item) for item in stored.data["files"] if item["kind"] == "part" and item["status"] == "completed"}
            index = MergedVideoInfo(stored.output_dir, stored.quality_directory(models.LEGACY_QUALITY, role='merged'))
            outputs = index.data['files']
            expected = [[1, 2, 3, 4, 5, 8, 10, 11, 12], [10, 11, 12], [3, 1, 3], [10, 11, 12, 1, 2, 3, 4, 5], [3, 1, 3]]
            names = ["p1-5_p8_p10-12", "p10-12", "p3_p1_p3", "p10-12_p1-5", "p3_p1_p3_2"]
            self.assertEqual([item["source_parts"] for item in outputs], expected)
            quality_suffix = '_' + stored.quality_suffix(models.LEGACY_QUALITY) if settings.mix_quality else ''
            self.assertEqual([item["file_name"] for item in outputs], [f"测试'投稿_{bvid}_{name}{quality_suffix}.mp4" for name in names])
            self.assertEqual(len({item["id"] for item in outputs}), 5)

            def frames(path: Path) -> list[str]:
                """读取 path 的实际解码帧并返回有序哈希列表，用于比较拼接前后的画面及顺序。"""
                result = subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return [line.rsplit(",", 1)[-1].strip() for line in result.stdout.splitlines() if line and not line.startswith("#")]

            input_frames = {part: frames(path) for part, path in inputs.items()}
            for item, sequence in zip(outputs, expected):
                self.assertEqual(frames(index.resolve_file(item)), [frame for part in sequence for frame in input_frames[part]])
                self.assertEqual(item["processing"], "stream copy")
                self.assertEqual(item["status"], "completed")
            self.assertTrue(all(path.is_file() for path in inputs.values()))
            original_ids = [item['id'] for item in outputs]
            original_sources = {path: path.read_bytes() for path in inputs.values()}
            with redirect_stdout(io.StringIO()):
                self.assertEqual(merge_service.merge_groups(settings, executable, stored, expression), 0)
            refreshed = MergedVideoInfo(stored.output_dir, index.path.parent)
            self.assertEqual([item['id'] for item in refreshed.data['files']], original_ids)
            for item, sequence in zip(refreshed.data['files'], expected):
                self.assertEqual(frames(refreshed.resolve_file(item)), [frame for part in sequence for frame in input_frames[part]])
            self.assertTrue(all(path.read_bytes() == content for path, content in original_sources.items()))
            self.assertEqual(list(record.output_dir.rglob('.merge-*')), [])
