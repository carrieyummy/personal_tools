"""离线验证链接、登录、字幕、续传与原编码无损封装；不读取真实账号或访问 B 站。"""

from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from bilibili_downloader.models import LEGACY_QUALITY, Quality
from bilibili_downloader import api, application, auth, errors, library, media, models, naming, quality, workflow, provenance
from bilibili_downloader import downloader as download_service
from bilibili_downloader import merge as merge_service
from bilibili_downloader import subtitles as subtitle_service
from bilibili_downloader import config as configuration
import download_bilibili as cli_entry
from bilibili_downloader import runtime
from tests import CONFIG_PATH, PROJECT_DIR, fake_auth_snapshot


class FakeResponse:
    """模拟接口响应，只提供本工具使用的 JSON、HTTP 状态和释放方法。"""

    def __init__(self, payload: dict, status: int = 200, url: str = "") -> None:
        """初始化 payload 响应、status 状态码及 url 最终地址；不返回值。"""
        self.payload, self.status, self.url = payload, status, url
        self.ok = 200 <= status < 300

    def json(self) -> dict:
        """返回预先设置的响应数据；无参数。"""
        return self.payload

    def dispose(self) -> None:
        """模拟释放响应；无参数、无返回值。"""


class DownloadTests(unittest.TestCase):
    """验证无需真实账户的业务逻辑及本地 HTTP 下载。"""

    def setUp(self) -> None:
        """创建本用例的临时目录及测试配置；无返回值。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name).resolve()
        self.settings = replace(configuration.load_settings(CONFIG_PATH), output_dir=self.folder, retries=0, threads=1)
        self.enterContext(patch.object(auth, 'snapshot_session', return_value=fake_auth_snapshot()))
        self.info = {"id": "BV1xx411c7mD_p1", "title": "测试视频", "http_headers": {}, "webpage_url": "https://www.bilibili.com/video/BV1xx411c7mD?p=1"}
        self.target = models.VideoTarget(self.info["webpage_url"], 123, 1)

    def tearDown(self) -> None:
        """移除本用例生成的临时文件；无返回值。"""
        self.temporary.cleanup()

    def test_default_config_is_in_script_config_directory(self) -> None:
        """验证默认 YAML 定位入口下的 config 目录，且文件迁移后仍可读取；无参数、无返回值。"""
        self.assertEqual(cli_entry.parse_args([]).config, CONFIG_PATH)
        self.assertTrue(CONFIG_PATH.is_file())

    def test_normalize_links_and_reject_other_content(self) -> None:
        """验证 BV/AV、分享文字、分 P 和不支持的链接；无返回值。"""
        self.assertEqual(api.normalize_url("BV1xx411c7mD"), "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(api.normalize_url("av123"), "https://www.bilibili.com/video/av123")
        self.assertEqual(api.normalize_url("分享 https://www.bilibili.com/video/BV1xx411c7mD/?p=2&share=1"), "https://www.bilibili.com/video/BV1xx411c7mD?p=2")
        self.assertEqual(api.normalize_url("https://b23.tv/abc"), "https://b23.tv/abc")
        for url in ("https://evil.example/video/BV1xx411c7mD", "https://www.bilibili.com/cheese/play/ep1", "https://live.bilibili.com/1", "https://www.bilibili.com/video/av1?p=0", "https://www.bilibili.com/video/av1?p=2&p=3"):
            with self.subTest(url=url), self.assertRaises(errors.ToolError):
                api.normalize_url(url)

    def test_parts_and_short_link(self) -> None:
        """验证默认第一 P、指定 P、全部 P 和短链接重定向；无返回值。"""
        pages = [{"page": 1, "cid": 10}, {"page": 2, "cid": 20}]
        context = MagicMock()
        context.request.get.return_value = FakeResponse({"code": 0, "data": pages})
        self.assertEqual([p.part for p in api.resolve_targets(context, "av123", self.settings, False)], [1])
        self.assertEqual([p.part for p in api.resolve_targets(context, "https://www.bilibili.com/video/av123?p=2", self.settings, False)], [2])
        self.assertEqual([p.part for p in api.resolve_targets(context, "https://www.bilibili.com/video/av123?p=2", self.settings, True)], [1, 2])
        with self.assertRaises(errors.ToolError):
            api.resolve_targets(context, "https://www.bilibili.com/video/av123?p=3", self.settings, False)
        context.request.get.side_effect = [FakeResponse({}, url="https://www.bilibili.com/video/av123?p=2"), FakeResponse({"code": 0, "data": pages})]
        self.assertEqual(api.resolve_targets(context, "https://b23.tv/abc", self.settings, False)[0].cid, 20)

    def test_login_reuses_and_renews(self) -> None:
        """验证有效登录复用、失效后等待重新登录及登录超时；无返回值。"""
        context = MagicMock()
        account = {"isLogin": True, "uname": "test"}
        with patch.object(auth, "login_status", return_value=account):
            self.assertEqual(auth.ensure_login(context, self.settings), account)
            context.new_page.assert_not_called()
        with patch.object(auth, "login_status", side_effect=[None, account]):
            self.assertEqual(auth.ensure_login(context, self.settings), account)
            context.pages[0].goto.assert_called_once()
        with patch.object(auth, "login_status", return_value=None), patch.object(auth.time, "monotonic", side_effect=[0, 1000]):
            with self.assertRaises(errors.ToolError):
                auth.ensure_login(context, self.settings)

    def test_cookie_injection_in_memory(self) -> None:
        """验证仅注入 B 站 Cookie，且没有登录 Cookie 时失败；无返回值。"""
        context = MagicMock()
        context.pages[0].evaluate.return_value = "test-agent"
        cookie = {"domain": ".bilibili.com", "name": "SESSDATA", "value": "fake-test-only", "path": "/", "secure": True, "expires": -1}
        context.cookies.return_value = [cookie, {**cookie, "domain": ".example.com", "name": "other"}]
        with download_service.make_downloader(context, self.settings) as downloader:
            self.assertEqual([item.name for item in downloader.cookiejar], ["SESSDATA"])
            self.assertEqual(downloader.params["fixup"], "never")
            self.assertEqual(downloader.params["postprocessors"], [])
            self.assertEqual(downloader.params["proxy"], "")
        context.cookies.return_value = []
        with self.assertRaises(errors.ToolError):
            download_service.make_downloader(context, self.settings)

    def test_quality_resolution_then_fps_and_platform_tier(self) -> None:
        """验证更高分辨率优于低分辨率高画质编号，同分辨率再比较帧率和档位；无返回值。"""
        from yt_dlp import YoutubeDL

        def selected(formats: list[dict]) -> str:
            """用实际 yt-dlp 对 formats 选流，返回最高视频格式 ID。"""
            with YoutubeDL({"quiet": True, "format": self.settings.format_selector, "format_sort": self.settings.format_sort}) as downloader:
                result = downloader.process_ie_result({"id": "test", "title": "test", "formats": formats}, download=False)
                return result["requested_formats"][0]["format_id"]

        formats = [
            {"format_id": "1080-dv", "url": "https://example.com/1", "ext": "mp4", "vcodec": "hevc", "acodec": "none", "width": 1920, "height": 1080, "fps": 60, "quality": 126, "dynamic_range": "DV"},
            {"format_id": "4k", "url": "https://example.com/2", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "width": 3840, "height": 2160, "fps": 30, "quality": 120},
            {"format_id": "audio", "url": "https://example.com/3", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "tbr": 192},
        ]
        self.assertEqual(selected(formats), "4k")
        formats.insert(0, {**formats[1], "format_id": "4k60", "fps": 60})
        self.assertEqual(selected(formats), "4k60")

    def test_subtitle_metadata_languages_ai_and_no_subtitles(self) -> None:
        """验证人工、AI、多语言、空列表及需要登录的字幕元数据；无返回值。"""
        context = MagicMock()
        entries = [{"id": 1, "lan": "zh-CN", "lan_doc": "中文", "subtitle_url": "//example.com/zh"}, {"id": 2, "lan": "ai-zh", "lan_doc": "中文 AI", "subtitle_url": "//example.com/ai"}, {"id": 3, "lan": "en", "subtitle_url": "//example.com/en"}]
        context.request.get.return_value = FakeResponse({"code": 0, "data": {"subtitle": {"subtitles": entries}}})
        tracks = subtitle_service.list_subtitles(context, self.target, self.info, replace(self.settings, include_ai=True))
        self.assertEqual([item.language for item in tracks], ["zh-CN", "ai-zh", "en"])
        self.assertTrue(tracks[1].is_ai)
        self.assertEqual(len(subtitle_service.list_subtitles(context, self.target, self.info, replace(self.settings, include_ai=False))), 2)
        self.assertEqual(len(subtitle_service.list_subtitles(context, self.target, self.info, replace(self.settings, include_ai=True, subtitle_languages=["zh.*", "ai-zh.*"]))), 2)
        context.request.get.return_value = FakeResponse({"code": 0, "data": {}})
        self.assertEqual(subtitle_service.list_subtitles(context, self.target, self.info, self.settings), [])
        context.request.get.return_value = FakeResponse({"code": 0, "data": {"need_login_subtitle": True}})
        with self.assertRaises(errors.ToolError):
            subtitle_service.list_subtitles(context, self.target, self.info, self.settings)

    def test_srt_timestamps_and_invalid_data(self) -> None:
        """验证毫秒进位、从零开始、中文内容及异常字幕时间；无返回值。"""
        self.assertEqual(subtitle_service.srt_timestamp(59.9996), "00:01:00,000")
        srt = subtitle_service.json_to_srt({"body": [{"from": 0, "to": 1.25, "content": "测试\n第二行"}]})
        self.assertEqual(srt, "1\n00:00:00,000 --> 00:00:01,250\n测试\n第二行\n")
        for data in ({}, {"body": [{"from": 2, "to": 1, "content": "x"}]}, {"body": [{"from": -1, "to": 1, "content": "x"}]}):
            with self.assertRaises(errors.ToolError):
                subtitle_service.json_to_srt(data)

    def test_subtitle_failure_preserves_success_and_fills_missing(self) -> None:
        """验证单条字幕失败不影响其他字幕，重复运行只补缺失字幕；无返回值。"""
        tracks = [models.SubtitleTrack("zh", "中文", False, "https://example.com/zh", "1"), models.SubtitleTrack("ai-en", "English", True, "https://example.com/en", "2")]
        body = {"body": [{"from": 0, "to": 1, "content": "text"}]}
        context = MagicMock()
        context.request.get.side_effect = [FakeResponse(body), FakeResponse({}, 500)]
        errors = subtitle_service.fetch_subtitles(context, tracks, self.folder, "video", self.settings)
        self.assertEqual(len(errors), 1)
        self.assertTrue((self.folder / "video.zh.srt").is_file())
        context.request.get.side_effect = [FakeResponse(body)]
        context.request.get.reset_mock()
        self.assertEqual(subtitle_service.fetch_subtitles(context, tracks, self.folder, "video", self.settings), [])
        context.request.get.assert_called_once()
        self.assertTrue((self.folder / "video.ai-en.ai.srt").is_file())

    def test_windows_names_and_config_paths(self) -> None:
        """验证 Windows 禁用字符、保留设备名、相对路径与配置校验；无返回值。"""
        import yaml

        self.assertEqual(naming.safe_name('A:B?C. ', 60), "A_B_C")
        self.assertEqual(naming.safe_name("CON", 60), "_CON")
        self.assertEqual(configuration.expand_path("downloads", self.folder), self.folder / "downloads")
        config_path = self.folder / "config.yaml"
        original = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        original['paths']['output_dir'] = 'relative'
        config_path.write_text(yaml.safe_dump(original), encoding="utf-8")
        self.assertEqual(configuration.load_settings(config_path).output_dir, self.folder / "relative")
        original['download']['retries'] = -1
        config_path.write_text(yaml.safe_dump(original), encoding="utf-8")
        with self.assertRaises(errors.ToolError):
            configuration.load_settings(config_path)
        config_path.write_text("paths: [", encoding="utf-8")
        with self.assertRaises(errors.ToolError):
            configuration.load_settings(config_path)

    def test_workspace_entries_match_script(self) -> None:
        """按配置的目录名称解析工作区变量，验证解释器、路径、测试发现目录及全部功能入口；无返回值。"""
        root = PROJECT_DIR.parent
        content = (root / "001_获取B站视频.code-workspace").read_text(encoding="utf-8")
        workspace = json.loads("\n".join(line for line in content.splitlines() if not line.lstrip().startswith("//")))
        workspace_roots = {'${workspaceFolder:' + folder.get('name', (root / folder['path']).resolve().name) + '}': str((root / folder['path']).resolve())
                           for folder in workspace['folders']}
        def resolve_workspace(value: str) -> str:
            """按当前工作区全部命名目录展开 value，返回可用于路径检查和参数解析的字符串。"""
            for variable, path in workspace_roots.items():
                value = value.replace(variable, path)
            return value
        configurations = workspace["launch"]["configurations"]
        self.assertEqual(len(configurations), 6)
        self.assertTrue(Path(workspace["settings"]["python.defaultInterpreterPath"]).is_file())
        settings = workspace["settings"]
        self.assertTrue(settings["python.testing.unittestEnabled"])
        test_cwd = Path(resolve_workspace(settings["python.testing.cwd"]))
        self.assertEqual(test_cwd.resolve(), PROJECT_DIR)
        test_arguments = settings["python.testing.unittestArgs"]
        self.assertEqual((test_cwd / test_arguments[test_arguments.index("-s") + 1]).resolve(), PROJECT_DIR / "tests")
        self.assertEqual((test_cwd / test_arguments[test_arguments.index("-t") + 1]).resolve(), PROJECT_DIR)
        for configuration in configurations:
            self.assertEqual(configuration["python"], "${config:python.defaultInterpreterPath}")
            self.assertTrue(Path(resolve_workspace(configuration["program"])).is_file())
            arguments = configuration["args"]
            config = arguments[arguments.index("--config") + 1]
            self.assertTrue(Path(resolve_workspace(config)).is_file())
            parsed = cli_entry.parse_args([resolve_workspace(value).replace("${input:bilibiliUrl}", "BV1xx411c7mD").replace("${input:bilibiliMergeId}", "BV1xx411c7mD").replace("${input:bilibiliMergeParts}", "1-5,8,10-12;10-12").replace("${input:bilibiliDownloadMergeParts}", "1-5,8,10-12;10-12") for value in arguments])
            self.assertEqual(parsed.login_only, configuration["name"] == "B站：验证 Chrome 登录态")
            self.assertEqual(parsed.list_formats, configuration["name"] == "B站：查看画质与字幕")
            self.assertEqual(parsed.all_parts, configuration["name"] == "B站：下载全部分 P")
            self.assertEqual(bool(parsed.merge_video), configuration["name"] == "B站：拼接已下载分 P")
            self.assertEqual(parsed.refresh_metadata, configuration["name"] == "B站：补生成视频信息")
            if parsed.merge_video or parsed.all_parts:
                self.assertEqual(parsed.merge_parts, "1-5,8,10-12;10-12")
        merge_input = next(item for item in workspace["launch"]["inputs"] if item["id"] == "bilibiliMergeParts")
        self.assertEqual(merge_input["default"], "0")
        download_input = next(item for item in workspace["launch"]["inputs"] if item["id"] == "bilibiliDownloadMergeParts")
        self.assertEqual(download_input["default"], "-1")
        task = next(task for task in workspace["tasks"]["tasks"] if task["label"] == "B站：准备环境")
        self.assertIn("--isolated", task["args"])
        self.assertEqual(task["command"], "${config:python.defaultInterpreterPath}")
        self.assertEqual(task["options"]["env"]["NO_PROXY"], "*")

    def test_cookie_export_uses_latest_and_limits_domains(self) -> None:
        """验证重复导出文件自动选最新、忽略外域并拒绝失效登录；无返回值。"""
        cookie = {"domain": ".bilibili.com", "name": "SESSDATA", "value": "fake-only", "path": "/", "secure": True}
        old, new = self.folder / "bilibili-cookies.json", self.folder / "bilibili-cookies (1).json"
        for path, agent in ((old, "old-agent"), (new, "new-agent")):
            path.write_text(json.dumps({"userAgent": agent, "cookies": [cookie, {**cookie, "domain": ".example.com"}]}), encoding="utf-8")
        os.utime(old, (1, 1))
        os.utime(new, (2, 2))
        path, agent, cookies = auth.load_cookie_export(self.folder / "bilibili-cookies*.json")
        self.assertEqual(path, new)
        self.assertEqual(agent, "new-agent")
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["expires"], -1)
        new.write_text(json.dumps({"userAgent": "agent", "cookies": [{**cookie, "expirationDate": 1}]}), encoding="utf-8")
        with self.assertRaises(errors.ToolError):
            auth.load_cookie_export(new)
        with self.assertRaises(errors.ToolError):
            auth.load_cookie_export(self.folder / "missing.json")

    def test_cookie_session_authenticates_without_browser(self) -> None:
        """验证默认 Chrome 导出可在无浏览器会话中认证、复用 UA 和 Cookie；无返回值。"""
        path = self.folder / "bilibili-cookies.json"
        cookie = {"domain": ".bilibili.com", "name": "SESSDATA", "value": "fake-only", "path": "/", "secure": True}
        path.write_text(json.dumps({"userAgent": "Chrome-test-agent", "cookies": [cookie]}), encoding="utf-8")
        response = MagicMock(status=200, url=api.API_ROOT + "/x/web-interface/nav")
        response.read.return_value = json.dumps({"code": 0, "data": {"isLogin": True, "uname": "test"}}).encode()
        with auth.authenticated_session(self.settings, path) as session:
            self.assertIsInstance(session, auth.CookieSession)
            with patch.object(session.downloader, "urlopen", return_value=response) as request:
                self.assertEqual(auth.ensure_login(session, self.settings)["uname"], "test")
                self.assertEqual(request.call_args.args[0].headers["User-Agent"], "Chrome-test-agent")
            with download_service.make_downloader(session, self.settings) as downloader:
                self.assertEqual(downloader.params["http_headers"]["User-Agent"], "Chrome-test-agent")
                self.assertEqual([item.name for item in downloader.cookiejar], ["SESSDATA"])
            with patch.object(auth, "login_status", return_value=None), patch.object(auth, "chrome_session") as browser:
                with self.assertRaises(errors.ToolError):
                    auth.ensure_login(session, self.settings)
                browser.assert_not_called()

    def test_resume_transfer_and_repeat_skip(self) -> None:
        """通过本地 Range HTTP 服务验证断点续传和完成后跳过，不访问外网；无返回值。"""
        from yt_dlp import YoutubeDL
        payload = b"original-encoded-stream" * 1024
        ranges = []

        class RangeHandler(BaseHTTPRequestHandler):
            """为本地下载测试提供支持 Range 的固定内容。"""

            def do_GET(self) -> None:
                """响应下载请求并记录 Range 请求头；无返回值。"""
                header = self.headers.get("Range")
                ranges.append(header)
                start = int(header.split("=")[1].split("-")[0]) if header else 0
                self.send_response(206 if header else 200)
                self.send_header("Content-Length", str(len(payload) - start))
                self.send_header("Content-Type", "application/octet-stream")
                if header:
                    self.send_header("Content-Range", f"bytes {start}-{len(payload)-1}/{len(payload)}")
                self.end_headers()
                self.wfile.write(payload[start:])

            def log_message(self, format: str, *args: object) -> None:
                """忽略 format/args 对应的 HTTP 访问日志；无返回值。"""

        server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        fmt = {"format_id": "raw", "url": f"http://127.0.0.1:{server.server_port}/video", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "protocol": "http", "http_headers": {}}
        base = naming.video_folder(self.settings, "测试视频", "BV1xx411c7mD")
        signature = [{key: fmt.get(key) for key in ('format_id', 'vcodec', 'acodec', 'width', 'height', 'fps')}]
        staging = base.with_name(base.name + '_' + LEGACY_QUALITY.directory + '_分P') / '.streams' / self.info['id'] / provenance.stream_version(1, signature)
        staging.mkdir(parents=True)
        (staging / "0.raw.mp4.part").write_bytes(payload[:512])
        info = {**self.info, "requested_formats": [fmt]}

        def finish(executable: str, sources: list[Path], formats: list[dict], folder: Path, stem: str) -> Path:
            """检查 sources 的续传结果，模拟 executable/formats 原样合并至 folder/stem，返回目标路径。"""
            self.assertEqual(sources[0].read_bytes(), payload)
            output = folder / f"{stem}.mp4"
            output.write_bytes(payload)
            return output

        try:
            with YoutubeDL({"quiet": True, "noprogress": True, "continuedl": True, "proxy": ""}) as downloader, patch.object(media, "mux_streams", side_effect=finish) as mux:
                download_service.save_video(downloader, info, self.settings, "unused", "测试视频", 2, source_cid=1)
                download_service.save_video(downloader, info, self.settings, "unused", "测试视频", 2, source_cid=1)
                mux.assert_called_once()
            self.assertIn("bytes=512-", ranges)
            self.assertFalse(staging.exists())
            record = library.get_video_info(self.settings, "BV1xx411c7mD")
            self.assertEqual(record.data["files"][0]["status"], "completed")
            self.assertEqual(list(self.folder.rglob("download.json")), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_download_failure_keeps_partial_stream(self) -> None:
        """按当前配置的实际传输路径验证中断后不封装、不删除断点文件；无返回值。"""
        downloader = MagicMock()
        fmt = {"format_id": "video", "ext": "mp4", "url": "https://example.com/video", "vcodec": "avc1", "acodec": "none"}
        partials = []

        def interrupted(filename: str, info: dict) -> tuple[bool, bool]:
            """模拟 filename/info 的中断传输，写入部分数据并返回失败标志。"""
            source = Path(filename)
            partial = source.with_name(source.name + '.part')
            partial.write_bytes(b"partial")
            partials.append(partial)
            return False, False

        downloader.dl.side_effect = interrupted
        with patch.object(media, "mux_streams") as mux, self.assertRaises(errors.ToolError):
            download_service.save_video(downloader, {**self.info, "requested_formats": [fmt]}, self.settings, "unused", "测试视频", 2, source_cid=1)
        self.assertEqual(len(partials), 1)
        self.assertTrue(partials[0].exists())
        record = library.get_video_info(self.settings, "BV1xx411c7mD")
        self.assertEqual(record.data["files"][0]["status"], "failed")
        mux.assert_not_called()

    def test_download_merge_selection_and_partial_failure(self) -> None:
        """验证下载拼接默认值、特殊选项、分组表达式和失败处理；模拟保存不落盘，拼接画质另行预选，无返回值。"""
        targets = [models.VideoTarget(f"https://www.bilibili.com/video/BV1xx411c7mD?p={part}", part, part, 2) for part in (1, 2)]
        cases = [(None, False, 0, 0, 0, 0), ("-1", False, 0, 0, 0, 0),
                 ("", False, 0, 0, 0, 0), ("   ", False, 0, 0, 0, 0),
                 ("0", False, 0, 1, 0, 0), ("0", True, 0, 0, 0, 2),
                 ("1,1;2,2", False, 0, 0, 1, 0), ("1,1;2,2", False, 2, 0, 1, 2),
                 ("1,1;2,2", True, 2, 0, 1, 2)]
        for case_index, (selection, failed, group_result, expected_merges, expected_groups, expected_exit) in enumerate(cases):
            with self.subTest(failed=failed, selection=selection, group_result=group_result):
                settings = replace(self.settings, output_dir=self.folder / str(case_index), priority='quality')
                arguments = ["--url", "BV1xx411c7mD", "--all-parts"]
                if selection is not None:
                    arguments.extend(["--merge-parts", selection])
                args = cli_entry.parse_args(arguments)
                session = MagicMock()
                session.__enter__.return_value = MagicMock()
                downloader = MagicMock()
                downloader.__enter__.return_value = downloader
                downloader.extract_info.side_effect = [{"id": f"BV1xx411c7mD_p{p}", "title": "test", 'formats': [
                    {'format_id': '80', 'quality': 80, 'url': 'https://example.com/video', 'ext': 'mp4',
                     'vcodec': 'avc1', 'acodec': 'none', 'width': 64, 'height': 64, 'fps': 5}]} for p in (1, 2, 1, 2)]
                def save(_downloader: object, info: dict, _settings: object, _title: str, _total: int, **options: object) -> object:
                    """检查传输前已有全部待下载条目，再模拟保存 info 分 P；其余参数对应下载配置，返回可供后处理的隔离替身。"""
                    pending = library.get_video_info(settings, "BV1xx411c7mD")
                    self.assertEqual({item["part"] for item in pending.data["files"]}, {1, 2})
                    self.assertTrue(all(item["status"] == "pending" for item in pending.data["files"]))
                    if failed and info["id"].endswith("p2"):
                        raise errors.ToolError("模拟失败")
                    return MagicMock(info=info, existing=None, queued_bytes=lambda: 0)
                with patch.object(configuration, "load_settings", return_value=settings), patch.object(media, "resolve_ffmpeg", return_value="ffmpeg"), patch.object(auth, "authenticated_session", return_value=session), patch.object(auth, "ensure_login", return_value={"uname": "test"}), patch.object(api, "resolve_targets", return_value=targets), patch.object(api, "api_json", return_value={"data": {"bvid": "BV1xx411c7mD", "title": "投稿标题", "owner": {"mid": 1, "name": "test"}}}), patch.object(download_service, "make_downloader", return_value=downloader), patch.object(subtitle_service, "list_subtitles", return_value=[]), patch.object(workflow, "print_video_info"), patch.object(download_service, "download_video", side_effect=save), patch.object(download_service, "finish_video", return_value=(self.folder, "test")), patch.object(subtitle_service, "fetch_subtitles", return_value=[]), patch.object(merge_service, "merge_parts") as merge, patch.object(merge_service, "merge_groups", return_value=group_result) as groups:
                    with patch.object(quality, 'choose_merge_quality', return_value=Quality(80, '1080P')), patch.object(quality, 'available_qualities', return_value={80: Quality(80, '1080P')}), patch.object(quality, 'confirm_ceiling', return_value=Quality(80, '1080P')), patch.object(quality, 'select_quality', side_effect=lambda _downloader, info, _quality: {**info, 'requested_formats': info['formats'], 'selected_quality': {'code': 80, 'name': '1080P'}}):
                        self.assertEqual(application.run(args), expected_exit)
                    self.assertEqual(merge.call_count, expected_merges)
                    self.assertEqual(groups.call_count, expected_groups)
                    if expected_groups:
                        self.assertEqual(groups.call_args.args[-1], selection)


class LosslessMuxTests(unittest.TestCase):
    """用现代 FFmpeg 的本地样例验证编码数据在封装前后保持一致。"""

    def test_merge_existing_parts_in_order_without_reencoding(self) -> None:
        """用含中文和单引号的旧目录生成两段视频，验证离线拼接顺序和编码帧；无返回值。"""
        settings = configuration.load_settings(CONFIG_PATH)
        executable = media.resolve_ffmpeg(settings, CONFIG_PATH.parent)
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(settings, output_dir=Path(directory) / "测试'目录")
            settings.output_dir.mkdir()
            manifests = []
            for part, color in ((1, "red"), (2, "blue")):
                folder = settings.output_dir / f"BV1xx411c7mD_p{part}"
                folder.mkdir()
                source = folder / f"片段'{part}.mp4"
                result = subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color={color}:size=64x64:rate=5", "-t", "1", "-c:v", "libx264", "-y", str(source)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = folder / "download.json"
                manifest.write_text(json.dumps({"id": f"BV1xx411c7mD_p{part}", "title": f"测试视频 p0{part} 片段", "file": source.name, "formats": [{"vcodec": "avc1", "acodec": "none", "width": 64, "height": 64}]}, ensure_ascii=False), encoding="utf-8")
                manifests.append(manifest)
            record = library.get_video_info(settings, "BV1xx411c7mD")
            record.data["files"].reverse()
            merged = merge_service.merge_parts(settings, executable, record)
            quality_suffix = '_' + LEGACY_QUALITY.directory
            directory_suffix = '' if settings.mix_quality else quality_suffix
            self.assertEqual(merged.parent.name, f'测试视频_BV1xx411c7mD{directory_suffix}_合并')
            self.assertEqual(merged.parent.parent, settings.output_dir.resolve())
            filename_suffix = quality_suffix if settings.mix_quality else ''
            self.assertEqual(merged.name, f"测试视频_BV1xx411c7mD{filename_suffix}.mp4")
            self.assertEqual(list(settings.output_dir.rglob("download.json")), [])
            from bilibili_downloader.merged_records import MergedVideoInfo
            merged_entry = MergedVideoInfo(settings.output_dir, merged.parent).data["files"][0]
            self.assertEqual(merged_entry["kind"], "merged")
            self.assertAlmostEqual(merged_entry["duration_seconds"], 2, places=1)
            self.assertEqual((merged_entry["width"], merged_entry["height"], merged_entry["fps"]), (64, 64, 5))
            def frames(path: Path) -> list[str]:
                """返回 path 视频的帧哈希列表，保留帧顺序。"""
                result = subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return [line.rsplit(",", 1)[-1].strip() for line in result.stdout.splitlines() if line and not line.startswith("#")]
            migrated_parts = {item["part"]: record.resolve_file(item) for item in record.data["files"] if item["kind"] == "part"}
            expected = frames(migrated_parts[1]) + frames(migrated_parts[2])
            self.assertEqual(frames(merged), expected)
            self.assertEqual(merge_service.merge_parts(settings, executable, record), merged)
            self.assertEqual(frames(merged), expected)
            self.assertEqual(len(MergedVideoInfo(settings.output_dir, merged.parent).data['files']), 1)

    def test_merge_rejects_missing_and_incompatible_parts(self) -> None:
        """验证缺失分 P 或编码不一致时不调用 FFmpeg；无返回值。"""
        settings = configuration.load_settings(CONFIG_PATH)
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(settings, output_dir=Path(directory))
            manifests = []
            for part in (1, 2):
                folder = settings.output_dir / str(part)
                folder.mkdir()
                filename = f'video{part}.mp4'
                (folder / filename).write_bytes(b"video")
                path = folder / "download.json"
                path.write_text(json.dumps({"id": f"BV1xx411c7mD_p{part}", "title": "test", "video_title": "test", "total_parts": 3, "file": filename, "formats": [{"vcodec": "avc1", "acodec": "none"}]}), encoding="utf-8")
                manifests.append(path)
            record = library.get_video_info(settings, "BV1xx411c7mD")
            with patch.object(runtime, "run_process") as ffmpeg, self.assertRaises(errors.ToolError):
                merge_service.merge_parts(settings, "ffmpeg", record)
            ffmpeg.assert_not_called()
            record.data["total_parts"] = 2
            record.data["files"][1]["formats"][0]["vcodec"] = "av01"
            record.save()
            with patch.object(runtime, "run_process") as ffmpeg, self.assertRaises(errors.ToolError):
                merge_service.merge_parts(settings, "ffmpeg", record)
            ffmpeg.assert_not_called()

    def test_mp4_and_mkv_preserve_encoded_packets(self) -> None:
        """生成测试源，验证 MP4/AAC 和 MKV/FLAC 的音视频编码包哈希相同；无返回值。"""
        settings = configuration.load_settings(CONFIG_PATH)
        executable = media.resolve_ffmpeg(settings, CONFIG_PATH.parent)

        def ffmpeg(arguments: list[str]) -> str:
            """用测试 FFmpeg 执行 arguments；成功返回标准输出，失败使本用例失败。"""
            result = subprocess.run([executable, "-hide_banner", "-nostdin", *arguments], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        def packet_hash(path: Path, stream: str) -> str:
            """返回 path 中 stream（v 或 a）编码包的 SHA256；使用 -c copy 保持压缩数据。"""
            return ffmpeg(["-i", str(path), "-map", f"0:{stream}:0", "-c", "copy", "-f", "hash", "-hash", "sha256", "-"]).strip()

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            video, aac, flac = folder / "video.mp4", folder / "audio.m4a", folder / "audio.flac"
            ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=64x64:rate=5", "-t", "1", "-c:v", "libx264", "-y", str(video)])
            for output, codec in ((aac, "aac"), (flac, "flac")):
                ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "1", "-c:a", codec, "-y", str(output)])
                formats = [{"vcodec": "avc1", "acodec": "none"}, {"vcodec": "none", "acodec": "mp4a" if codec == "aac" else "flac"}]
                result = media.mux_streams(executable, [video, output], formats, folder, codec)
                self.assertEqual(result.suffix, ".mp4" if codec == "aac" else ".mkv")
                self.assertEqual(packet_hash(video, "v"), packet_hash(result, "v"))
                self.assertEqual(packet_hash(output, "a"), packet_hash(result, "a"))

    def test_mp4_mux_failure_uses_mkv_without_encoder(self) -> None:
        """模拟 MP4 不兼容，确认第二次仅用 copy 改封装 MKV；无返回值。"""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            formats = [{"vcodec": "avc1", "acodec": "mp4a"}]
            commands = []

            def invoke(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
                """记录 command，第一次失败、第二次写入目标；kwargs 为进程选项，返回模拟执行结果。"""
                commands.append(command)
                if len(commands) == 1:
                    return subprocess.CompletedProcess(command, 1, "", "container unsupported")
                Path(command[-1]).write_bytes(b"muxed")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(runtime, "run_process", side_effect=invoke):
                result = media.mux_streams("ffmpeg", [folder / "source"], formats, folder, "result")
            self.assertEqual(result.suffix, ".mkv")
            for command in commands:
                self.assertEqual(command[command.index("-c") + 1], "copy")
                self.assertNotIn("-vf", command)


class ChromeExportTests(unittest.TestCase):
    """在临时无头 Chrome 中验证导出弹窗，用模拟 Cookie，不读取任何真实浏览器配置。"""

    def test_local_extension_export_and_missing_login(self) -> None:
        """点击真实弹窗验证 JSON 下载及未登录提示；Cookie API 使用测试替身，无返回值。"""
        from playwright.sync_api import sync_playwright
        extension = PROJECT_DIR / "chrome_cookie_export"
        manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["permissions"], ["cookies"])
        self.assertEqual(manifest["host_permissions"], ["https://*.bilibili.com/*"])
        html = (extension / "popup.html").read_text(encoding="utf-8").replace('<script src="popup.js"></script>', "")
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server"])
            try:
                page = browser.new_page(accept_downloads=True)
                page.set_content(html)
                page.evaluate("window.chrome = { cookies: { getAll: async (options) => { window.requestedDomain = options.domain; return [{domain: '.bilibili.com', name: 'SESSDATA', value: 'fake-test-only', path: '/', secure: true}]; } } }")
                page.add_script_tag(path=str(extension / "popup.js"))
                with page.expect_download() as event:
                    page.locator("#export").click()
                download = event.value
                path = Path(directory) / download.suggested_filename
                download.save_as(path)
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(download.suggested_filename, "bilibili-cookies.json")
                self.assertEqual(payload["cookies"][0]["value"], "fake-test-only")
                self.assertEqual(page.evaluate("window.requestedDomain"), "bilibili.com")
                page.evaluate("window.chrome.cookies.getAll = async () => []")
                page.locator("#export").click()
                page.wait_for_function("document.getElementById('status').textContent.includes('没有找到')")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
