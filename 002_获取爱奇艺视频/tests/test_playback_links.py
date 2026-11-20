"""验证链接清单、电视剧目录和独立电影解析，不访问网络或下载媒体。"""

import base64
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import yaml

from qsv_local.playback.application import build_plan
from qsv_local.playback.listing import expand_episodes, task_options
from qsv_local.playback.metadata import linked_videos, require_same_metadata
from qsv_local.playback.source import Source, validate_selected_source


class PlaybackLinksTests(unittest.TestCase):
    """验证链接列表、全局设置及网页所需的节目元数据。"""

    def test_link_resolves_to_album_name_in_plan(self):
        """模拟官方短链与元数据，验证清单 URL 到专辑名和剧集名的传递；无返回值。"""
        root = Path(__file__).resolve().parents[1]
        playback = yaml.safe_load((root / "configs/playback.yaml").read_text(encoding="utf-8-sig"))
        listing = yaml.safe_load((root / "download_list/download_list.yaml").read_text(encoding="utf-8-sig"))
        network = playback["network"]
        host = network["allowed_hosts"][0]
        video = {"tvId": 123, "albumId": 456, "order": 1, "albumName": "示例专辑", "name": "示例第一集",
                 "vid": "fixture", "payMark": False, "duration": 100}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.yaml"
            listing.update({"all-parts": False, "download_list": [f"https://{host}/?tvid=123"]})
            path.write_text(yaml.safe_dump(listing, allow_unicode=True), encoding="utf-8")
            cache = MagicMock()
            cache.get.return_value = {"release_date": "2000-01-01", "album_publish_time": "2000-01-01"}
            profile = {"download_list": path, "main_config": root / "configs/config.yaml", "network": network, "_album_cache": cache}
            redirect = MagicMock()
            redirect.__enter__.return_value.url = listing["download_list"][0]
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({"code": "A00000", "data": video}).encode()
            with patch("qsv_local.playback.metadata.open_network", side_effect=[redirect, response]):
                jobs, errors = build_plan(profile, None)
            self.assertFalse(errors)
            self.assertEqual(jobs[0]["video"]["title"], video["albumName"])
            self.assertEqual(jobs[0]["video"]["episode_title"], video["name"])
            self.assertEqual(jobs[0]["video"]["album_id"], str(video["albumId"]))
            self.assertEqual(jobs[0]["video"]["play_url"], listing["download_list"][0])

    def test_catalog_preserves_each_episode_page(self):
        """全剧目录的各集沿用官方返回的页面地址，不继承清单中的单集地址；无返回值。"""
        root = Path(__file__).resolve().parents[1]
        profile = yaml.safe_load((root / "configs/playback.yaml").read_text(encoding="utf-8-sig"))
        host = profile["network"]["allowed_hosts"][0]
        rows = [{"effective": True, "contentType": 1, "order": n, "tvId": n, "vid": str(n),
                 "payMark": False, "duration": 100, "name": str(n), "playUrl": f"https://{host}/?tvid={n}"}
                for n in (1, 2)]
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"code": "A00000", "data": {
            "albumId": "456", "epsodelist": rows, "hasMore": False}}).encode()
        with patch("qsv_local.playback.listing.open_network", return_value=response):
            result = expand_episodes(profile, {"album_id": "456", "play_url": f"https://{host}/original"}, True)
        self.assertEqual([item["play_url"] for item in result], [row["playUrl"] for row in rows])

    def test_invalid_entries_are_rejected(self):
        """旧对象、空链接和首尾空白均被拒绝；无返回值。"""
        root = Path(__file__).resolve().parents[1]
        listing = yaml.safe_load((root / "download_list/download_list.yaml").read_text(encoding="utf-8-sig"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.yaml"
            for invalid in ({"url": "https://example.invalid"}, "", " https://example.invalid"):
                listing["download_list"] = [invalid]
                path.write_text(yaml.safe_dump(listing, allow_unicode=True), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "非空链接字符串"):
                    task_options({"download_list": path, "main_config": root / "configs/config.yaml"})


class MovieLinksTests(unittest.TestCase):
    """核验电影短链接、直接播放页、单任务计划及发布前的上映日复核。"""

    def setUp(self):
        """从实际配置构造网络替身、合成电影和含两个链接的临时清单，无返回值。"""
        root = Path(__file__).resolve().parents[1]
        playback = yaml.safe_load((root / "configs/playback.yaml").read_text(encoding="utf-8-sig"))
        network = playback["network"]
        listing = yaml.safe_load((root / "download_list/download_list.yaml").read_text(encoding="utf-8-sig"))
        base = f"https://{network['allowed_hosts'][0]}"
        # 使用高于配置旧编号阈值的合成 TVID，直接验证普通播放页的配置解码。
        number = network["play_url_id"]["legacy_threshold"] + 222
        self.movie = {"tvId": number, "albumId": number, "order": 1, "albumName": "合成电影", "name": "合成电影",
                      "vid": "movie", "payMark": 1, "duration": "01:40:00", "durationSec": 6000, "publishDate": "20200821",
                      "formatIssueTime": "2021-04-11", "channelId": 1, "contentType": 1, "effective": True,
                      "solo": True, "videoCount": 1}
        self.clip = {"tvId": 111, "albumId": 333, "order": 1, "albumName": "合成专辑", "name": "合成素材",
                     "vid": "clip", "payMark": 0, "duration": "02:00", "contentType": 3, "featureVideoId": self.movie["tvId"]}
        query = urlencode({key: base64.b64encode(str(row["tvId"]).encode("ascii")).decode("ascii")
                           for key, row in (("shareId", self.clip), ("positiveId", self.movie))})
        self.short = f"{base}/movie-short"
        self.redirect = f"{base}/playShare.html?{query}"
        value, encoded = number ^ network["play_url_id"]["xor_mask"], ""
        while value:
            value, remainder = divmod(value, 36)
            encoded = "0123456789abcdefghijklmnopqrstuvwxyz"[remainder] + encoded
        self.direct = f"{base}/v_{encoded}.html"
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / "links.yaml"
        listing.update({"all-parts": True, "download_list": [self.short, self.direct]})
        path.write_text(yaml.safe_dump(listing, allow_unicode=True), encoding="utf-8")
        self.cache = MagicMock()
        self.profile = {**playback, "download_list": path, "main_config": root / "configs/config.yaml", "_album_cache": self.cache}
        self.enterContext(patch("qsv_local.playback.metadata.open_network", side_effect=self.respond))
        self.catalog = self.enterContext(patch("qsv_local.playback.listing.open_network"))

    def respond(self, profile: dict, request):
        """处理 profile 的 request，返回定向链接或合成节目响应；未知地址使测试失败。"""
        context = MagicMock()
        result = context.__enter__.return_value
        if request.full_url in (self.short, self.direct):
            result.url = self.redirect if request.full_url == self.short else self.direct
        else:
            template = profile["network"]["video_info_url"]
            rows = {template.format(tvid=item["tvId"]): item for item in (self.clip, self.movie)}
            result.read.return_value = json.dumps({"code": "A00000", "data": rows[request.full_url]}).encode()
        return context

    def test_short_and_direct_share_one_movie_job(self):
        """两类链接解析为同一电影，整剧设置只生成一条任务，并以集序 1 匹配。"""
        jobs, errors = build_plan(self.profile, None, [1])
        self.assertFalse(errors)
        self.assertEqual(len(jobs), 1)
        video = jobs[0]["video"]
        self.assertEqual(video["tvid"], str(self.movie["tvId"]))
        self.assertTrue(video["is_movie"])
        self.assertEqual(video["release_date"], "2020-08-21")
        self.assertNotIn("album_publish_time", video)
        self.cache.get.assert_not_called()
        self.catalog.assert_not_called()
        other_jobs, other_errors = build_plan(self.profile, [0], [2])
        self.assertFalse(other_jobs)
        self.assertIn("不含所指定的集序", other_errors[0]["error"])

    def test_share_requires_official_feature_match(self):
        """关联缺失或与 positiveId 冲突时，拒绝将素材映射为电影正片。"""
        for feature in (None, self.clip["tvId"]):
            with self.subTest(feature=feature):
                self.clip["featureVideoId"] = feature
                jobs, errors = build_plan(self.profile, [0])
                self.assertFalse(jobs)
                self.assertIn("positiveId 与节目资料关联不一致", errors[0]["error"])
        self.cache.get.assert_not_called()
        self.catalog.assert_not_called()

    def test_movie_rejects_bad_date_or_duration(self):
        """电影院线上映日无效和公开时长冲突均失败，无返回值。"""
        original = self.movie.copy()
        for changes, message in (({"publishDate": "20200230"}, "publishDate"),
                                 ({"durationSec": original["durationSec"] + 1}, "时长声明不一致")):
            with self.subTest(changes=changes):
                self.movie.update(original | changes)
                jobs, errors = build_plan(self.profile, [1])
                self.assertFalse(jobs)
                self.assertIn(message, errors[0]["error"])

    def test_movie_release_date_is_checked_again(self):
        """正式复核接受同一正片元数据，电影上映日变化时停止当前任务。"""
        video = linked_videos(self.profile, [0])[0]
        require_same_metadata(self.profile, video)
        self.movie["publishDate"] = "20200822"
        with self.assertRaisesRegex(RuntimeError, "元数据发生变化"):
            require_same_metadata(self.profile, video)

    def test_movie_preview_and_incomplete_manifest_are_rejected(self):
        """试看授权或比公开时长短的清单无法作为电影正片，无返回值。"""
        video = linked_videos(self.profile, [0])[0]
        selection = self.profile["playback"]["selection"]
        tolerance = self.profile["playback"]["media_download"]["duration_tolerance_seconds"]
        candidate = {"vid": video["vid"], "bid": 1, "br": 1, "fr": 1, "dr": 0, "_selected": True,
                     "code": selection["supported_codecs"][0], "ff": selection["supported_formats"][0],
                     "drm": {"code": "A00000", "preview": True, "ticket": "synthetic"}}
        source = Source({}, {"data": {"program": {"video": [candidate]}}}, candidate, b"", {})
        with patch("qsv_local.playback.source.fetch_manifest", return_value=(b"synthetic", {})) as fetch:
            with self.assertRaisesRegex(RuntimeError, "试看"):
                validate_selected_source(self.profile, video, 1, source)
            fetch.assert_not_called()
            candidate["drm"]["preview"] = False
            video["duration_seconds"] = tolerance + 100
            with patch("qsv_local.playback.source.parse_media_manifest", return_value={"duration": 99}):
                with self.assertRaisesRegex(RuntimeError, "播放清单不是完整正片"):
                    validate_selected_source(self.profile, video, 1, source)
