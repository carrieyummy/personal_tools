"""隔离验证链接任务、画质排名、来源断点和成品复用；不请求公网或控制客户端。"""

import json
import hashlib
import gzip
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from qsv_local.playback.application import build_plan
from qsv_local.playback.coordination import StageControls
from qsv_local.playback.download import download_steps, manifest_identity
from qsv_local.playback.listing import task_options
from qsv_local.playback.media import MediaNetworkError, fetch_segment, save_segment
from qsv_local.playback.outputs import reusable_output, target_path
from qsv_local.playback.records import PlaybackDirectoryIndex
from qsv_local.playback.source import Source, candidate_groups, select_source, tier_key
from qsv_local.storage import atomic_json, fingerprint


class PlaybackDownloadTests(unittest.TestCase):
    """从当前配置构造合成输入，检查影响完整性、画质选择和本地文件保护的边界。"""

    def setUp(self):
        """加载当前适配规则并创建无真实账号的合成节目、原生候选；无返回值。"""
        self.root = Path(__file__).resolve().parents[1]
        self.raw = yaml.safe_load((self.root / "configs/playback.yaml").read_text(encoding="utf-8"))
        self.video = {"index": 0, "tvid": "123", "album_id": "456", "title": "合成剧集", "episode": 1,
                      "episode_title": "合成节目", "vid": "abcdef", "duration_seconds": 20, "ischarge": False,
                      "release_date": "2000-01-02", "album_publish_time": 946742400000}
        settings = self.raw["playback"]
        self.source = {"vid": "synthetic_source", "bid": settings["request"]["bitstream"], "dr": settings["request"]["hdr_type"],
                       "br": 1, "fr": max(1, settings["request"]["frame_rate"]), "code": settings["selection"]["codec_preference"][0],
                       "ff": settings["selection"]["supported_formats"][0], "_selected": True, "zqyh": 1,
                       "vsize": 20, "scrsz": "16x16", "duration": 20, "mver": "synthetic"}

    def test_quality_groups_follow_config_and_keep_same_tier_variants_together(self):
        """同档不同源不重复占用排名，较强的所有排序字段位于更高组；无返回值。"""
        settings = self.raw["playback"]["selection"]
        low = dict(self.source, vid="low", dr=0, zqyh=0)
        high = dict(low, vid="high", zqyh=1, bid=low["bid"] + 1, br=low["br"] + 1, fr=low["fr"] + 1)
        duplicate = dict(high, vid="same_tier")
        groups = candidate_groups([low, high, duplicate], settings)
        self.assertEqual(len(groups), 2)
        self.assertEqual({item["vid"] for item in groups[0]}, {high["vid"], duplicate["vid"]})
        self.assertGreater(tier_key(high, settings), tier_key(low, settings))

    def test_unsupported_top_tier_does_not_download_lower_tier(self):
        """最高组不可处理时明确失败，不把较低组当作最高档；无返回值。"""
        low = dict(self.source, dr=0, zqyh=0)
        unsupported = dict(self.source, bid=low["bid"] + 1, br=low["br"] + 1, fr=low["fr"] + 1, vid="unsupported", ff="unsupported_format")
        result = Source({}, {"data": {"program": {"video": [low, unsupported]}}}, low, b"", {})
        with patch("qsv_local.playback.source.request_source", return_value=result) as request:
            with self.assertRaisesRegex(ValueError, "不自动降低"):
                select_source(self.raw, self.video, 1)
        self.assertEqual(request.call_count, 1)

    def test_source_repr_does_not_expose_session_material(self):
        """内存资源对象的默认日志表示不显示响应、临时地址或请求头；无返回值。"""
        result = Source({"mode": "safe"}, {"secret": "private_payload"}, {"ml": "private_url"}, b"private_body", {"pck": "private_pck"})
        for value in ("private_payload", "private_url", "private_body", "private_pck"):
            self.assertNotIn(value, repr(result))

    def test_unverified_rank_is_rejected_before_native_request(self):
        """未验证排名在调用 SDK 前明确拒绝，避免将其他返回源当作请求档位；无返回值。"""
        unsupported = max(self.raw["playback"]["selection"]["supported_ranks"]) + 1
        with patch("qsv_local.playback.source.request_source") as request:
            with self.assertRaisesRegex(ValueError, "精确选档尚未适配"):
                select_source(self.raw, self.video, unsupported)
        request.assert_not_called()

    def response(self, data: bytes, url: str, content_range: str | None = None):
        """为测试构造包含 data、url 及可选 content_range 的可关闭 HTTP 响应；返回 BytesIO 替身。"""
        response = BytesIO(data)
        response.status = 206 if content_range else 200
        response.url = url
        response.headers = {"Content-Range": content_range}
        return response

    def test_segment_uses_http_range_and_preserves_authentication(self):
        """字节区间必须使用 HTTP Range，URL 中的分段参数移除而原生授权参数保持原样；无返回值。"""
        host = "synthetic" + self.raw["playback"]["resource_host_suffix"]
        url = "https://" + host + "/part.ts?start=10&end=14&contentlength=4&qd_sc=synthetic"
        response = self.response(b"abcd", url, "bytes 10-13/20")
        with patch("qsv_local.playback.media.open_network", return_value=response) as network:
            data, _ = fetch_segment(self.raw, url, {"User-Agent": "synthetic"}, self.raw["playback"]["media_probe"]["max_segment_bytes"])
        request = network.call_args.args[1]
        self.assertEqual(request.get_header("Range"), "bytes=10-13")
        self.assertEqual(request.full_url, "https://" + host + "/part.ts?qd_sc=synthetic")
        self.assertEqual(data, b"abcd")

    def test_wrong_content_range_is_rejected(self):
        """字节数相同但服务端返回另一范围时必须失败；无返回值。"""
        host = "synthetic" + self.raw["playback"]["resource_host_suffix"]
        url = "https://" + host + "/part.ts?start=10&end=14"
        with patch("qsv_local.playback.media.open_network", return_value=self.response(b"abcd", url, "bytes 0-3/20")):
            with self.assertRaisesRegex(RuntimeError, "Content-Range"):
                fetch_segment(self.raw, url, {}, self.raw["playback"]["media_probe"]["max_segment_bytes"])

    def test_ignored_range_cannot_be_saved_as_a_valid_segment(self):
        """节点忽略区间并返回整文件时，不保存为短段；无返回值。"""
        host = "synthetic" + self.raw["playback"]["resource_host_suffix"]
        url = "https://" + host + "/part.ts?start=10&end=14"
        with patch("qsv_local.playback.media.open_network", return_value=self.response(b"entire_file_content", url)):
            with self.assertRaisesRegex(RuntimeError, "未遵守"):
                fetch_segment(self.raw, url, {}, self.raw["playback"]["media_probe"]["max_segment_bytes"])

    def test_full_download_streams_segments_larger_than_sampling_limit(self):
        """正常长段可超过抽样上限，完整下载按配置块长读取且摘要正确；无返回值。"""
        main = yaml.safe_load((self.root / "configs/config.yaml").read_text(encoding="utf-8"))
        total = self.raw["playback"]["media_probe"]["max_segment_bytes"] + 1
        buffer_bytes = main["copy_buffer_bytes"]
        host = "synthetic" + self.raw["playback"]["resource_host_suffix"]
        url = "https://" + host + f"/part.ts?start=0&end={total}"
        response = self.response(b"x" * total, url, f"bytes 0-{total - 1}/{total}")
        reads, original_read = [], response.read

        def bounded_read(size):
            """记录并执行 size 字节读取；返回原响应内容，拒绝无界请求。"""
            reads.append(size)
            self.assertGreater(size, 0)
            self.assertLessEqual(size, max(32, buffer_bytes))
            return original_read(size)

        response.read = bounded_read
        with tempfile.TemporaryDirectory() as directory, patch("qsv_local.playback.media.open_network", return_value=response):
            target = Path(directory) / "segment.ts"
            digest = save_segment(self.raw, url, {}, target, total, buffer_bytes)
            self.assertEqual(digest["size_bytes"], total)
            self.assertEqual(digest["sha256"], hashlib.sha256(b"x" * total).hexdigest())
            self.assertEqual(fingerprint(target, buffer_bytes, force=True), digest)
        self.assertGreater(len(reads), 2)

    def test_binary_segment_with_dispatch_like_prefix_streams_as_media(self):
        """完整音轨块及范围媒体即使与调度文本有相似开头，也按清单长度保存；无返回值。"""
        total = self.raw["network"]["max_response_bytes"] + 1
        buffer_bytes = yaml.safe_load((self.root / "configs/config.yaml").read_text(encoding="utf-8"))["copy_buffer_bytes"]
        host = "synthetic" + self.raw["playback"]["resource_host_suffix"]
        url = f"https://{host}/part?start=0&end={total}"
        cases = [
            (b'{"\xff', "whole", None, {}),
            (b'{"l":"maybe text', "whole", None, {"Content-Type": "application/octet-stream", "Content-Length": str(total)}),
            (b'var videoUrl={code:', "header", f"bytes 0-{total - 1}/{total}", {}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for index, (prefix, style, content_range, headers) in enumerate(cases):
                with self.subTest(style=style, prefix=prefix):
                    data = prefix.ljust(total, b"x")
                    response = self.response(data, url, content_range)
                    response.headers.update(headers)
                    target = Path(directory) / f"segment_{index}.ts"
                    with patch("qsv_local.playback.media.open_network", return_value=response) as network:
                        result = save_segment(self.raw, url, {}, target, total, buffer_bytes, range_style=style)
                    network.assert_called_once()
                    self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
                    self.assertEqual(target.stat().st_size, total)

    def test_segment_follows_json_and_native_text_dispatch(self):
        """已适配的 JSON 和原生脚本字面量继续完成一次调度；无返回值。"""
        host = "synthetic" + self.raw["playback"]["resource_host_suffix"]
        url = f"https://{host}/dispatch?start=0&end=128"
        resolved = f"https://{host}/media"
        payloads = (
            json.dumps({"l": resolved}).encode(),
            ('var videoUrl={code:"A00000",data:{l:"' + resolved + '"}};').encode(),
            f'var resolved = "{resolved}";'.encode(),
        )
        for dispatch in payloads:
            with self.subTest(form=dispatch[:4]):
                data = b"x" * 128
                responses = [self.response(dispatch, url), self.response(data, resolved)]
                with patch("qsv_local.playback.media.open_network", side_effect=responses) as network:
                    actual, _ = fetch_segment(self.raw, url, {}, len(data), range_style="whole")
                self.assertEqual(actual, data)
                self.assertEqual(network.call_count, 2)
                self.assertEqual(network.call_args.args[1].full_url, resolved)

    def test_oversized_dispatch_still_rejected(self):
        """结构符合调度文本的响应仍受配置字节上限约束；无返回值。"""
        limit = self.raw["network"]["max_response_bytes"]
        host = "synthetic" + self.raw["playback"]["resource_host_suffix"]
        url = f"https://{host}/dispatch?start=0&end=128"
        response = self.response(b'{"l":"' + b"x" * limit + b'"}', url)
        with patch("qsv_local.playback.media.open_network", return_value=response) as network:
            with self.assertRaisesRegex(ValueError, "媒体调度响应超过配置上限"):
                fetch_segment(self.raw, url, {}, 128, range_style="whole")
        network.assert_called_once()

    def test_inline_hls_requires_206_range_proof_before_saving_segment(self):
        """即使收到的长度正确，内嵌 HLS 也须收到对应 HTTP 206 范围才保存分段；无返回值。"""
        host = "synthetic" + self.raw["playback"]["resource_host_suffix"]
        url = "https://" + host + "/part.ts?start=10&end=14"
        main = yaml.safe_load((self.root / "configs/config.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "segment.ts"
            with patch("qsv_local.playback.media.open_network", return_value=self.response(b"abcd", url)):
                with self.assertRaisesRegex(RuntimeError, "HTTP 206"):
                    save_segment(self.raw, url, {}, target, 4, main["copy_buffer_bytes"], require_content_range=True)
                self.assertFalse(target.exists())
            with patch("qsv_local.playback.media.open_network", return_value=self.response(b"abcd", url, "bytes 10-13/20")):
                saved = save_segment(self.raw, url, {}, target, 4, main["copy_buffer_bytes"], require_content_range=True)
            self.assertEqual(saved["size_bytes"], 4)
            self.assertEqual(target.read_bytes(), b"abcd")

    def test_truncated_segment_is_not_published_or_recorded(self):
        """网络截断时仅保留未完成文件，不能发布有效分段；无返回值。"""
        host = "synthetic" + self.raw["playback"]["resource_host_suffix"]
        url = "https://" + host + "/part.ts?start=10&end=14"
        response = self.response(b"ab", url, "bytes 10-13/20")
        with tempfile.TemporaryDirectory() as directory, patch("qsv_local.playback.media.open_network", return_value=response):
            target = Path(directory) / "segment.ts"
            with self.assertRaises(MediaNetworkError):
                save_segment(self.raw, url, {}, target, 4, 64)
            self.assertFalse(target.exists())
            self.assertEqual(len(list(target.parent.glob("*.partial"))), 1)

    def test_resume_identity_ignores_rotating_signatures_but_rejects_changed_ranges(self):
        """临时票据变化不破坏可靠断点，媒体范围变化必须生成新来源；无返回值。"""
        host = "synthetic" + self.raw["playback"]["resource_host_suffix"]
        first = [{"url": "https://" + host + "/part.ts?start=0&end=20&qd_sc=private_one", "duration": 20}]
        second = [{**first[0], "url": first[0]["url"].replace("private_one", "private_two")}]
        profile = {"playback": self.raw["playback"]}
        identity = manifest_identity(profile, self.video, self.source, first)
        self.assertEqual(identity, manifest_identity(profile, self.video, self.source, second))
        second[0]["url"] = second[0]["url"].replace("end=20", "end=19")
        self.assertNotEqual(identity, manifest_identity(profile, self.video, self.source, second))
        self.assertNotIn("private_one", json.dumps(identity))

    def test_checkpoint_retries_only_temporary_permission_failures(self):
        """瞬时替换占用按配置重试，耗尽时保留既有记录；无返回值。"""
        logging = yaml.safe_load((self.root / "configs/config.yaml").read_text(encoding="utf-8"))["logging"]
        logging = {**logging, "write_retries": max(1, logging["write_retries"])}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "checkpoint.json"
            atomic_json(target, {"old": True})
            replace = Path.replace
            attempts = []

            def occupied_once(path, destination):
                """首次对 path/destination 模拟外部占用，之后真实替换并返回目标路径。"""
                attempts.append(path)
                if len(attempts) == 1:
                    raise PermissionError("合成瞬时占用")
                return replace(path, destination)

            with patch.object(Path, "replace", occupied_once), patch("qsv_local.storage.time.sleep") as delay:
                atomic_json(target, {"new": True}, retry_settings=logging)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"new": True})
            delay.assert_called_once_with(logging["write_retry_interval_seconds"])
            with patch.object(Path, "replace", side_effect=PermissionError("合成持续占用")), patch("qsv_local.storage.time.sleep"):
                with self.assertRaises(PermissionError):
                    atomic_json(target, {"invalid": True}, retry_settings=logging)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"new": True})
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_download_yields_before_processing_and_then_publishes(self):
        """完整下载在分片就绪时让出容量，恢复后封装、上报验证设备并发布；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = yaml.safe_load((self.root / "configs/config.yaml").read_text(encoding="utf-8"))
            config.update(output_path=str(root / "output"), sdk={}, ffmpeg_path=Path("ffmpeg"))
            profile = {"main_config": root / "main.yaml", "playback": self.raw["playback"]}
            source = dict(self.source, ml="https://synthetic.invalid/list", drm={"ticket": "synthetic"})
            url = "https://synthetic.invalid/part.ts?start=0&end=20"
            plan = {"format": "hls", "tracks": [], "duration": 20, "segments": [{"url": url, "duration": 20}]}
            selection = {"rank": 1, "native_selection_verified": True}
            work = root / "work"
            controls = StageControls(config)
            events = []

            def save(profile, url, headers, path, size, buffer_bytes, cancel, *, progress=None):
                """保存接口参数指定的合成分片，progress 可通知累计字节，返回实际摘要。"""
                path.write_bytes(b"x" * size)
                if progress:
                    progress(size)
                return fingerprint(path, buffer_bytes, force=True)

            def convert(outer, clear, progress, buffer_bytes):
                """模拟授权处理 outer 到 clear，progress 为进度回调，返回帧及编码证明。"""
                clear.write_bytes(outer.read_bytes())
                progress(1, 1)
                return {"stream_type": 0x1b, "video_pes": 1}

            def process(command, log_path, config, *args, **kwargs):
                """按媒体进程接口创建封装候选或解码日志，无返回值。"""
                if "-movflags" in command:
                    Path(command[-1]).write_bytes(b"verified media")
                elif command == ["decode"]:
                    self.assertEqual(events[-1]["stage"], "verifying")
                    self.assertIn(next(event["device"] for event in reversed(events) if "device" in event), controls.devices)

            media = {"width": 16, "height": 16, "errors": [], "duration_seconds": 20}
            with (patch("qsv_local.playback.download.media_config", return_value=config),
                  patch("qsv_local.playback.download.selected_media_plan", return_value=plan),
                  patch("qsv_local.playback.download.signed_segment", return_value=url),
                  patch("qsv_local.playback.download.save_segment", side_effect=save),
                  patch("qsv_local.playback.processing.NativeV5"),
                  patch("qsv_local.playback.processing.TransportDecoder") as decoder,
                  patch("qsv_local.playback.download.run_process", side_effect=process),
                  patch("qsv_local.playback.download.probe_media", return_value=media),
                  patch("qsv_local.playback.download.decode_command", return_value=["decode"]),
                  patch("qsv_local.playback.download.validate_progress", return_value={"decode_exit_code": 0, "frames": 1}),
                  patch("qsv_local.playback.publication.require_same_metadata")):
                decoder.return_value.convert.side_effect = convert
                steps = download_steps(profile, self.video, source, b"manifest", work, {}, selection, controls, events.append)
                self.assertEqual(next(steps), ("download", 0))
                self.assertEqual(next(steps), ("ready", source["vsize"]))
                self.assertFalse((work / "outer.ts").exists())
                with self.assertRaises(StopIteration) as result:
                    next(steps)
            self.assertEqual(result.exception.value["status"], "completed")
            self.assertEqual(next(event["device"] for event in events if "device" in event), result.exception.value["verification"]["device"])
            self.assertEqual(Path(result.exception.value["output"]).read_bytes(), b"verified media")

    def test_listing_overrides_main_options(self):
        """简单链接列表共享清单设置，清单全剧设置优先于主配置；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main_value, global_quality = True, 3
            listing = {"quality": global_quality, "all-parts": not main_value,
                       "download_list": ["https://synthetic.invalid"]}
            main, path = root / "main.yaml", root / "links.yaml"
            main.write_text(yaml.safe_dump({"download": {"all_parts": main_value}}), encoding="utf-8")
            path.write_text(yaml.safe_dump(listing), encoding="utf-8")
            profile = {"main_config": main, "download_list": path}
            values = task_options(profile)[0]
            self.assertEqual(values["quality"], listing["quality"])
            self.assertEqual(values["all_parts"], listing["all-parts"])
            del listing["all-parts"]
            path.write_text(yaml.safe_dump(listing), encoding="utf-8")
            self.assertEqual(task_options(profile)[0]["all_parts"], main_value)

    def test_hls_uses_independent_selected_audio_in_final_mux(self):
        """HLS 视频与独立 AMP4 资源共同计量，最终只映射精确选定音轨；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = yaml.safe_load((self.root / "configs/config.yaml").read_text(encoding="utf-8"))
            config.update(output_path=str(root / "output"), sdk={}, ffmpeg_path=Path("ffmpeg"))
            profile = {"main_config": root / "main.yaml", "playback": self.raw["playback"]}
            audio = gzip.compress(b"\0\0\0\x10ftypM4A " + b"\0" * 4)
            audio_url = f"https://{self.video['tvid']}.invalid/audio.amp4?start=0&end={len(audio)}"
            video_url = "https://synthetic.invalid/video.ts?start=0&end=20"
            source = {**self.source, "drm": {"ticket": "synthetic"},
                      "_audio_selection": {"rank": 1, "codec": "aac", "native_selection_verified": True},
                      "_audio_segments": [{"url": audio_url, "start": 0, "end": len(audio), "duration": None,
                                           "headers": {"User-Agent": "synthetic"}, "range_style": "whole", "audio_asset": True}]}
            plan = {"format": "hls", "tracks": [], "duration": 20, "segments": [{"url": video_url, "duration": 20}]}
            commands = []

            def save(profile, url, headers, path, size, buffer_bytes, cancel, **options):
                """保存 url 指定的合成资源到 path，核对音轨 headers/options；返回实际指纹。"""
                if url == audio_url:
                    self.assertEqual(options["range_style"], "whole")
                    self.assertEqual(headers, source["_audio_segments"][0]["headers"])
                path.write_bytes(audio if url == audio_url else b"x" * size)
                options["progress"](size)
                return fingerprint(path, buffer_bytes, force=True)

            def hls(config, source, rows, paths, work, controls, progress):
                """为参数指定的视频 paths 创建合成 clear 输入并返回样本计数。"""
                self.assertEqual(len(paths), 1)
                target = work / "clear.ts"
                target.write_bytes(b"synthetic video")
                return [target], {"container": "ts", "video_codec": "h264", "video_pes": 1}

            def process(command, log_path, config, *args, **kwargs):
                """记录 command 并创建封装候选，无返回值。"""
                commands.append(command)
                if "-movflags" in command:
                    Path(command[-1]).write_bytes(b"synthetic selected media")

            audio_media = {"duration_seconds": 20, "audio_codecs": ["aac"], "audio_layouts": ["stereo"], "errors": []}
            media = {**audio_media, "width": 16, "height": 16}
            with (patch("qsv_local.playback.download.media_config", return_value=config),
                  patch("qsv_local.playback.manifests.parse_media_manifest", return_value=plan),
                  patch("qsv_local.playback.download.signed_segment", return_value=video_url),
                  patch("qsv_local.playback.download.save_segment", side_effect=save),
                  patch("qsv_local.playback.download.process_hls", side_effect=hls),
                  patch("qsv_local.playback.download.run_process", side_effect=process),
                  patch("qsv_local.playback.download.probe_media", side_effect=[audio_media, media]),
                  patch("qsv_local.playback.download.decode_command", return_value=["decode"]),
                  patch("qsv_local.playback.download.validate_progress", return_value={"decode_exit_code": 0, "frames": 1}),
                  patch("qsv_local.playback.publication.require_same_metadata")):
                steps = download_steps(profile, self.video, source, b"", root / "work", {}, {"rank": 1, "native_selection_verified": True}, StageControls(config))
                self.assertEqual(next(steps), ("download", 0))
                self.assertEqual(next(steps), ("ready", self.source["vsize"] + len(audio)))
                with self.assertRaises(StopIteration) as result:
                    next(steps)
            self.assertTrue(result.exception.value["highest_audio_verified"])
            self.assertIn("1:a:0", commands[0])
            self.assertNotIn("0:a", commands[0])

    def test_episode_filter_cannot_escape_task_expansion(self):
        """CLI 集序过滤不能把 all-parts=false 的任务扩成其他集；无返回值。"""
        options = [{"index": 0, "quality": 1, "all_parts": False, "note": ""}]
        with patch("qsv_local.playback.application.task_options", return_value=options), patch(
                "qsv_local.playback.application.linked_videos", return_value=[self.video]):
            jobs, errors = build_plan({}, [0], [self.video["episode"] + 1])
        self.assertFalse(jobs)
        self.assertEqual(len(errors), 1)

    def test_duplicate_tasks_do_not_duplicate_media_jobs(self):
        """同视频同画质在不同链接重复出现时仅保留一次；无返回值。"""
        options = [{"index": index, "quality": 1, "all_parts": False, "note": ""} for index in range(2)]
        with patch("qsv_local.playback.application.task_options", return_value=options), patch(
                "qsv_local.playback.application.linked_videos", return_value=[self.video]):
            jobs, errors = build_plan({}, None)
        self.assertEqual(len(jobs), 1)
        self.assertFalse(errors)

    def test_reuse_checks_registered_filename_and_size_without_reading_media(self):
        """来源、预期命名和登记长度匹配才复用，同尺寸改写不会触发摘要或探测；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            main = yaml.safe_load((self.root / "configs/config.yaml").read_text(encoding="utf-8"))
            main["output_path"] = str(root / "output")
            main["work_dir"] = str(root / "work")
            profile = {"main_config": root / "main.yaml", "playback": self.raw["playback"]}
            target = target_path(main, profile["main_config"], self.video, self.source)
            target.parent.mkdir(parents=True)
            original = b"synthetic_verified_media"
            target.write_bytes(original)
            identity, selection = {"sha256": "synthetic_identity"}, {"rank": 1, "native_selection_verified": True}
            data = {"record_type": "playback_download", "schema_version": 1, "status": "completed", "source_identity": identity,
                    "verification": {"decode_exit_code": 0, "frames": 1}, "output_fingerprint": fingerprint(target, main["copy_buffer_bytes"]),
                    "media_verified": True, "output": str(target), "media": {"width": 16, "height": 16}}
            index = PlaybackDirectoryIndex(target.parent, Path(main["output_path"]).resolve())
            index.add(target.name, data)
            wrong_name = target.with_name("不同文件名.mp4")
            wrong_name.write_bytes(original)
            index.add(wrong_name.name, {**data, "output": str(wrong_name)})
            index.save()
            with (patch("qsv_local.playback.outputs.require_same_metadata"),
                  patch("hashlib.sha256", side_effect=AssertionError("复用时不应重算摘要")),
                  patch("qsv_local.process.run_process", side_effect=AssertionError("复用时不应探测媒体"))):
                target.write_bytes(b"x" * len(original))
                result = reusable_output(profile, main, self.video, self.source, identity, selection)
                self.assertEqual((result["status"], result["output"]), ("reused", str(target)))
                self.assertEqual(result["reuse_check"], "filename_and_size")
                self.assertEqual(result["output_fingerprint"], data["output_fingerprint"])
                self.assertEqual(result["media"], data["media"])
                changed = original[:-1]
                target.write_bytes(changed)
                self.assertIsNone(reusable_output(profile, main, self.video, self.source, identity, selection))
                self.assertEqual(target.read_bytes(), changed)


if __name__ == "__main__":
    unittest.main()
