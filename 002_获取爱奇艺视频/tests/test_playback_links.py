"""验证简单链接清单解析及官方名称传递，不访问网络或下载媒体。"""

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import yaml

from qsv_local.playback.application import build_plan
from qsv_local.playback.listing import expand_episodes, task_options


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
