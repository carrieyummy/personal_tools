"""使用当前可编辑配置和回环 HTTP 验证清单、下载、失败保护、复用及发布事务。"""

from __future__ import annotations

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

import yaml

from download_mgtv import parse_args
from local.activity import page
from local.client import Transport, normalize_url
from local.client import NativeClient
from local.configuration import read_config
from local.errors import AddressExpired, AuthenticationError, PlaybackDenied, SourceChanged, ToolError
from local.hls import download, parse_media, playlists
from local.storage import DirectoryLock, Publisher, digest, file_hash
from local.tasks import Request, catalog, choose, requests
from local.tasks import streams
from local.media import base, mux, process, verify


ROOT = Path(__file__).resolve().parents[1]


class BaseCase(unittest.TestCase):
    """创建独立工作目录；从当前主配置读取默认值，测试只覆盖所需行为。"""

    def setUp(self):
        """准备本用例配置与临时目录；无参数/返回值。"""
        self.temporary = TemporaryDirectory(prefix="mgtv_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = read_config(ROOT / "configs" / "config.yaml")
        self.config.update(output_path=self.root / "out", work_dir=self.root / "work")
        self.cancel = Event()


class ManifestTests(BaseCase):
    """清单继承、原站目录、画质选择与 CLI 约束。"""

    def test_list_precedence(self):
        """简单链接共享清单设置，CLI 覆盖整剧开关；非法类型和旧对象格式被拒绝。"""
        link = yaml.safe_load(self.config["default_list"].read_text(encoding="utf-8-sig"))["download_list"][0]
        config = self.config
        global_rank = config["download"]["quality"] + 1
        document = {"quality": global_rank, "all-parts": not config["download"]["all_parts"],
                    "download_list": [link, link]}
        path = self.root / "list.yaml"
        path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        tasks = requests([str(path)], config, None)
        self.assertEqual([task.quality for task in tasks], [global_rank, global_rank])
        self.assertEqual([task.all_parts for task in tasks], [document["all-parts"], document["all-parts"]])
        self.assertTrue(all(task.all_parts for task in requests([str(path)], config, True)))
        document["quality"] = True
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        with self.assertRaises(ToolError):
            requests([str(path)], config, None)
        document["quality"] = global_rank
        for invalid in ({"url": link}, "", " " + link):
            document["download_list"] = [invalid]
            path.write_text(yaml.safe_dump(document), encoding="utf-8")
            with self.assertRaises(ToolError):
                requests([str(path)], config, None)

    def test_source_order_not_numeric_definition(self):
        """采用接口档位顺序；数字较大的 definition 不等于较高清晰度；无参数/返回值。"""
        source = {"videoSources": [{"definition": "8", "fileId": "a", "fileHash": "aa"}, {"definition": "101", "fileId": "b", "fileHash": "bb"}]}
        self.assertEqual(choose(source, 1)["fileId"], "a")
        with self.assertRaises(ToolError):
            choose(source, 3)

    def test_nonmember_does_not_rank_paid_options(self):
        """非会员明示需付费档不占可下载排名；无参数/返回值。"""
        source = {"user": {"isVip": 0}, "videoSources": [{"definition": "101", "fileId": "paid", "fileHash": "a", "needPay": 1},
                                                              {"definition": "4", "fileId": "free", "fileHash": "b", "needPay": 0}]}
        self.assertEqual(choose(source, 1)["fileId"], "free")
        source["user"]["isVip"] = 1
        self.assertEqual(choose(source, 1)["fileId"], "paid")

    def test_catalog_pagination_and_main_only(self):
        """分页保序、只取正片、保留待上线状态；重复页必须失败；无参数/返回值。"""
        def row(number, kind=1, status=1):
            """构造目录条目；number/kind/status 为编号、类型和上线状态；返回接口字典。"""
            return {"clipId": "1", "videoId": str(number), "videoIndex": number, "name": str(number), "status": status, "type": kind}
        first = {"pageTotal": 2, "total": 2, "clipInfo": {"clipId": "1", "clipName": "原站"}, "list": [row(1), row(2, 2)]}
        second = {**first, "list": [row(3, status=0)]}
        from unittest.mock import Mock
        client = Mock()
        client.series_release_date.return_value = "2000-01-02"
        client.catalog_page.side_effect = [first, second]
        result = catalog(client, Request("https://www.mgtv.com/b/1/1.html", 1, True))
        self.assertEqual([item.video_id for item in result], ["1", "3"])
        self.assertFalse(result[-1].released)
        client.catalog_page.side_effect = [first, first]
        with self.assertRaises(ToolError):
            catalog(client, Request("https://www.mgtv.com/b/1/1.html", 1, True))

    def test_cli_and_url(self):
        """无链接保持交互语义；互斥模式返回参数错误；拒绝伪造域名；无参数/返回值。"""
        self.assertIsNone(parse_args([]).url)
        with self.assertRaises(SystemExit) as caught:
            parse_args(["--login-only", "--list-formats"])
        self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(ToolError):
            normalize_url("https://mgtv.com.example.org/b/1/2.html")

    def test_dispatch_preserves_selected_source(self):
        """内嵌调度优先，缺失时只访问同源URL，失败不选择备用源；无参数/返回值。"""
        client = NativeClient(self.config, self.cancel)
        stream = {"fileId": "chosen", "fileHash": "stable", "url": "http://disp.mgtv.com/vod.do?fid=chosen&pm=private",
                  "bakStreams": [{"fileId": "lower", "fileHash": "other"}]}
        from unittest.mock import Mock
        client.transport.json = Mock(return_value=({"status": "ok", "info": "https://cdn.mgtv.com/video.m3u8"}, {"http_status": 200}))
        result = client.dispatch(stream)
        self.assertEqual(result["fileId"], "chosen")
        self.assertEqual(client.transport.json.call_args.args[1]["fid"], "chosen")
        self.assertTrue(client.transport.json.call_args.args[0].startswith("https://"))
        client.transport.json.reset_mock()
        self.assertEqual(client.dispatch(result), result)
        client.transport.json.assert_not_called()
        client.transport.json.return_value = ({"status": "error", "code": 617}, {"http_status": 617})
        with self.assertRaises(ToolError):
            client.dispatch(stream)

    def test_login_validity_is_separate_from_episode_permissions(self):
        """有效票据但单集无权限仍能验证登录；HTTP认证失败明确停止；无参数/返回值。"""
        from unittest.mock import Mock
        client = NativeClient(self.config, self.cancel)
        ticket_state = self.config["profile"]["status"]["valid_ticket_states"][0]
        client.raw_source = Mock(return_value=({"code": "denied_fixture", "data": {"user": {"tstatus": ticket_state, "isVip": 1}}},
                                              {"http_status": 200, "message": "单集权限限制"}))
        self.assertEqual(client.login_status("1", "2")["isVip"], 1)
        with self.assertRaises(PlaybackDenied):
            client.source("1", "2")
        authentication_status = self.config["profile"]["status"]["authentication_http_codes"][0]
        client.raw_source.return_value = ({}, {"http_status": authentication_status})
        with self.assertRaises(AuthenticationError):
            client.source("1", "2")


class HLSTests(BaseCase):
    """真实回环 HTTP 和稳定清单身份，用短数据验证重试/断点而非外部网络。"""

    def setUp(self):
        """建立无日志 HTTP 服务，按 routes 映射返回资源；无参数/返回值。"""
        super().setUp()
        self.routes, self.hits = {}, []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            """可控的测试服务器，支持显式返回状态、响应头与数据。"""

            def do_GET(self):
                """按测试映射返回资源；无参数/返回值。"""
                path = self.path.split("?", 1)[0]
                owner.hits.append((self.path, self.headers.get("Range")))
                code, headers, content = owner.routes.get(path, (404, {}, b""))
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format, *args):
                """关闭测试 HTTP 日志；format/args 为标准库参数；无返回值。"""

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/index.m3u8"
        self.transport = Transport(self.config["network"], self.config["profile"]["status"]["expired_http_codes"], self.cancel)

    def media(self, suffix=""):
        """构造完整双分片清单；suffix 为签名查询部分；返回解析对象。"""
        return parse_media(f"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\na.ts{suffix}\n#EXTINF:2,\nb.ts{suffix}\n#EXT-X-ENDLIST\n", self.url)

    def test_resume_hash_and_signature_refresh(self):
        """恢复时校验分片并统计断点字节，签名变化不重下且不计其他现场文件；篡改分片重下；无参数/返回值。"""
        self.routes.update({"/a.ts": (200, {}, b"aaa"), "/b.ts": (200, {}, b"bbb")})
        directory = self.root / "segments"
        call = lambda media: download(self.transport, [media], directory, self.config, lambda: [media], lambda *args: None, self.cancel)
        expected_bytes = sum(len(self.routes[path][2]) for path in ("/a.ts", "/b.ts"))
        self.assertEqual(call(self.media("?token=first"))[1]["bytes"], expected_bytes)
        (directory / "candidate.mp4").write_bytes(b"previous candidate")
        (directory / "track_0" / "unused.bin").write_bytes(b"historical fragment")
        self.hits.clear()
        self.assertEqual(call(self.media("?token=second"))[1]["bytes"], expected_bytes)
        self.assertFalse(self.hits)
        (directory / "track_0" / "000000.bin").write_bytes(b"bad")
        self.assertEqual(call(self.media())[1]["bytes"], expected_bytes)
        self.assertEqual(len(self.hits), 1)
        self.assertNotIn("token", (directory / "segments.json").read_text())

    def test_expired_refresh_and_changed_identity(self):
        """失效地址只续签同一清单；清单变化时保留断点并报错；无参数/返回值。"""
        expired = self.config["profile"]["status"]["expired_http_codes"][0]
        self.routes.update({"/a.ts": (expired, {}, b""), "/b.ts": (200, {}, b"bbb")})
        calls = []

        def refresh():
            """模拟同源续签；无参数；返回相同稳定身份的新地址清单。"""
            calls.append(True)
            self.routes["/a.ts"] = (200, {}, b"aaa")
            return [self.media("?new=1")]

        download(self.transport, [self.media()], self.root / "good", self.config, refresh, lambda *args: None, self.cancel)
        self.assertEqual(len(calls), 1)
        self.routes["/a.ts"] = (expired, {}, b"")
        changed = parse_media("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nchanged.ts\n#EXT-X-ENDLIST", self.url)
        with self.assertRaises(SourceChanged):
            download(self.transport, [self.media()], self.root / "bad", self.config, lambda: [changed], lambda *args: None, self.cancel)
        self.assertFalse(list((self.root / "bad").rglob("*.part")))

    def test_complete_and_encryption_guards(self):
        """缺少 ENDLIST、加密及不完整 EXTINF 均不能当作完整正片；无参数/返回值。"""
        texts = ["#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\na.ts", "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-KEY:METHOD=SAMPLE-AES\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST",
                 "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n#EXT-X-ENDLIST"]
        for text in texts:
            with self.subTest(text=text), self.assertRaises(ToolError):
                parse_media(text, self.url)

    def test_fmp4_range_and_separate_audio(self):
        """主清单展开独立音轨，fMP4 初始化和精确范围下载；无参数/返回值。"""
        master = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="中文",URI="audio.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=100,AUDIO="a"\nvideo.m3u8\n'
        video = '#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:2,\n#EXT-X-BYTERANGE:3@2\nvideo.mp4\n#EXT-X-ENDLIST'
        audio = '#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\naudio.aac\n#EXT-X-ENDLIST'
        self.routes.update({"/index.m3u8": (200, {}, master.encode()), "/video.m3u8": (200, {}, video.encode()),
                            "/audio.m3u8": (200, {}, audio.encode()), "/init.mp4": (200, {}, b"init"),
                            "/video.mp4": (206, {"Content-Range": "bytes 2-4/10"}, b"vid"), "/audio.aac": (200, {}, b"aud")})
        selected = playlists(self.transport, self.url)
        self.assertEqual(len(selected), 2)
        paths, evidence = download(self.transport, selected, self.root / "fmp4", self.config, lambda: selected, lambda *args: None, self.cancel)
        self.assertEqual(evidence["resources"], 3)
        self.assertEqual(evidence["bytes"], sum(len(self.routes[path][2]) for path in ("/init.mp4", "/video.mp4", "/audio.aac")))
        self.assertEqual(len(paths), 2)
        self.assertIn(("/video.mp4", "bytes=2-4"), self.hits)
        self.assertIn("#EXT-X-MAP", paths[0].read_text())

    def test_pipeline_real_media_publication_and_reuse(self):
        """通过正式入口运行回环 HLS、真实封装/完整解码/发布，再验证指纹复用和清理；无参数/返回值。"""
        from types import SimpleNamespace
        from unittest.mock import Mock
        from urllib.parse import urlsplit

        from local.application import run
        from local.tasks import Episode

        directory = self.root / "synthetic"
        directory.mkdir()
        generated = directory / "generated.m3u8"
        command = base(self.config) + ["-f", "lavfi", "-i", "testsrc2=size=64x64:rate=5:duration=2",
                                      "-f", "lavfi", "-i", "sine=frequency=1000:duration=2", "-c:v", "libx264", "-g", "5", "-c:a", "aac",
                                      "-f", "hls", "-hls_time", "1", "-hls_segment_filename", str(directory / "original_%03d.ts"), str(generated)]
        process(command, directory / "generate.log", self.config, self.cancel)
        media = parse_media(generated.read_text(), self.url)
        self.routes["/index.m3u8"] = (200, {}, generated.read_bytes())
        for resource in media.resources:
            path = urlsplit(resource.url).path
            self.routes[path] = (200, {}, (directory / Path(path).name).read_bytes())
        self.config["decode_verification"].update(cpu=True, gpu=False)
        self.config["download"]["keep_intermediates"] = False
        self.config["logging"]["port"] = 0
        episode = Episode("1", "2", 1, "合成单集", "合成剧集", True, self.config["download"]["quality"], release_date="2000-01-02")
        stream = {"fileId": "synthetic", "fileHash": "synthetic_hash", "definition": "fixture", "ftime": str(media.duration),
                  "standardName": "测试画质", "videoWidth": "64", "videoHeight": "64", "videoFormat": "H264", "audioFormat": "AAC",
                  "disp": {"info": self.url}}
        args = SimpleNamespace(config=self.config["config_path"], url=["https://www.mgtv.com/b/1/2.html"],
                               all_parts=False, login_only=False, list_formats=False)
        with patch("local.application.read_config", return_value=self.config), \
                patch("local.application.NativeClient", return_value=Mock(transport=self.transport)), \
                patch("local.application.catalog", return_value=[episode]), patch("local.application.resolved", return_value=stream):
            self.assertEqual(run(args), 0)
            self.hits.clear()
            self.assertEqual(run(args), 0)
        index = json.loads((self.config["output_path"] / self.config["logging"]["index_file"]).read_text(encoding="utf-8"))
        records = index["videos"][episode.key]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(Path(record["path"]).parts[0], self.config["naming"]["series_date_prefix"].format(release_date=episode.release_date) + self.config["naming"]["series_template"].format(title=episode.series, clip_id=episode.clip_id))
        self.assertNotIn(episode.release_date, Path(record["path"]).name)
        self.assertEqual(record["hls"]["bytes"], sum(len(self.routes[urlsplit(item.url).path][2]) for item in media.resources))
        self.assertEqual(record["verification"]["exit_code"], 0)
        self.assertGreater(record["verification"]["decoded_frames"], 0)
        self.assertTrue((self.config["output_path"] / record["path"]).is_file())
        state = json.loads((self.config["output_path"] / self.config["logging"]["state_file"]).read_text(encoding="utf-8"))
        self.assertEqual(state["runs"][-1]["items"][episode.key]["stage"], "复用")
        self.assertEqual(self.hits, [("/index.m3u8", None)])
        self.assertFalse(list(self.config["work_dir"].rglob("candidate.mp4")))
        self.assertFalse(list(self.config["work_dir"].rglob("track_*")))


class StorageTests(BaseCase):
    """跨进程排他、无覆盖发布、事务回滚和完整指纹复用。"""

    def test_publish_versions_reuse_and_unknown_file(self):
        """未知同名文件保留，记录匹配才能复用，内容篡改后不能复用；无参数/返回值。"""
        publisher = Publisher(self.config, self.cancel)
        source = self.root / "candidate.mp4"
        source.write_bytes(b"verified")
        target = self.config["output_path"] / "series" / "ep.mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"unknown")
        record = publisher.publish(source, Path("series/ep.mp4"), "1/2", "identity", {"verification": {"exit_code": 0, "decoded_frames": 1, "decoded_seconds": 1}})
        self.assertEqual(target.read_bytes(), b"unknown")
        self.assertNotEqual(record["path"], "series/ep.mp4")
        self.assertEqual(publisher.reusable("1/2", "identity"), record)
        self.assertIsNone(publisher.reusable("1/2", "other"))
        (publisher.root / record["path"]).write_bytes(b"tampered")
        self.assertIsNone(publisher.reusable("1/2", "identity"))

    def test_index_failure_rolls_back_only_new_file(self):
        """索引提交失败撤回本次发布，不改未知文件；无参数/返回值。"""
        publisher = Publisher(self.config, self.cancel)
        source = self.root / "candidate.mp4"
        source.write_bytes(b"verified")
        with patch("local.storage.atomic_json", side_effect=OSError("index unavailable")):
            with self.assertRaises(OSError):
                publisher.publish(source, Path("series/ep.mp4"), "1/2", "identity", {})
        self.assertFalse((publisher.root / "series/ep.mp4").exists())
        self.assertFalse(publisher.index["videos"].get("1/2"))

    def test_lock_and_html_injection(self):
        """同目录锁拒绝第二使用者，离线报告安全展示任意源标题；无参数/返回值。"""
        path = self.root / self.config["logging"]["lock_file"]
        with DirectoryLock(path):
            with self.assertRaises(ToolError):
                with DirectoryLock(path):
                    pass
        with DirectoryLock(path):
            pass
        output = page({"runs": [{"title": "</script><script>alert(1)</script>"}]}, self.config["logging"]["refresh_interval_seconds"], False)
        self.assertNotIn("</script><script>", output)


class MediaTests(BaseCase):
    """用当前配置 FFmpeg 生成小媒体，验证实际 TS/fMP4 无损封装及全轨解码。"""

    def test_real_ts_and_fmp4_complete_decode(self):
        """两种容器都经过真实 FFmpeg 封装和完整解码，不能用元数据替身证明成功；无参数/返回值。"""
        from local.hls import local_manifest
        for kind in ("mpegts", "fmp4"):
            with self.subTest(kind=kind):
                directory = self.root / kind
                directory.mkdir()
                generated = directory / "generated.m3u8"
                command = base(self.config) + ["-f", "lavfi", "-i", "testsrc2=size=64x64:rate=5:duration=2", "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
                                               "-c:v", "libx264", "-g", "5", "-c:a", "aac", "-f", "hls", "-hls_time", "1", "-hls_segment_type", kind,
                                               "-hls_segment_filename", str(directory / "original_%03d.ts"), str(generated)]
                process(command, directory / "generate.log", self.config, self.cancel)
                parsed = parse_media(generated.read_text(), generated.as_uri())
                track = directory / "local"
                track.mkdir()
                from urllib.request import url2pathname
                from urllib.parse import urlsplit
                for index, resource in enumerate(parsed.resources):
                    path = Path(url2pathname(urlsplit(resource.url).path))
                    (track / f"{index:06d}.bin").write_bytes(path.read_bytes())
                manifest = local_manifest(parsed, track)
                candidate = directory / "candidate.mp4"
                try:
                    mux(self.config, [manifest], candidate, self.cancel)
                except ToolError:
                    print((directory / "mux.log").read_text(encoding="utf-8", errors="replace"))
                    raise
                result = verify(self.config, candidate, "cpu", [parsed.duration], {"videoWidth": "64", "videoHeight": "64", "audioFormat": "AAC"}, self.cancel)
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(len(result["actual_media"]["audio_codecs"]), 1)
                self.assertEqual(result["actual_media"]["video_codec"], "h264")


if __name__ == "__main__":
    unittest.main()
