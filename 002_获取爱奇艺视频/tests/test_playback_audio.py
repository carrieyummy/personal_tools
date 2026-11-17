"""隔离核对独立音轨选档、调度路径和最终媒体计划，不连接客户端。"""

from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import yaml

from qsv_local.playback.audio import audio_groups, audio_segments
from qsv_local.playback.manifests import selected_media_plan


class AudioTests(unittest.TestCase):
    """使用实际适配配置及合成媒体，验证音质排名和资源身份边界。"""

    def setUp(self):
        """读取当前配置构造安全的合成域名；无返回值。"""
        self.profile = yaml.safe_load((Path(__file__).resolve().parents[1] / "configs/playback.yaml").read_text(encoding="utf-8"))
        self.host = "synthetic" + self.profile["playback"]["resource_host_suffix"]

    def test_highest_rank_uses_native_tiers_and_matching_language(self):
        """文件较大不能覆盖原生高档位，其他配音不混排；无返回值。"""
        rows = [{"aid": str(i), "bid": i, "ct": 1, "asize": 10 - i, "_match": 1} for i in (1, 2)]
        rows += [{"aid": "other", "bid": 99, "ct": 1, "_match": 0}]
        groups = audio_groups(rows, self.profile["playback"]["audio_selection"])
        self.assertEqual(groups[0][0]["aid"], "2")
        self.assertEqual(len(groups), 2)

    def test_audio_dispatch_preserves_native_videos_prefix(self):
        """/v0 相对路径不能吞掉原生 /videos 前缀，全零时长按字节资源处理；无返回值。"""
        selected = {"asize": 10, "fs": [{"i": 1, "b": 10, "d": 0, "l": "/v0/test.amp4?qd_sc=synthetic"}]}
        rows = audio_segments(self.profile, selected, 30, "https://" + self.host + "/videos")
        self.assertEqual(urlsplit(rows[0]["url"]).path, "/videos/v0/test.amp4")
        self.assertEqual(rows[0]["range_style"], "whole")
        self.assertIsNone(rows[0]["duration"])
        self.assertEqual(rows[0]["end"], selected["asize"])

    def test_equal_tier_preserves_native_order_and_video_language(self):
        """同档不能用 AID 哈希排序，且 _match 不是配音语言证明；无返回值。"""
        rows = [{"aid": "z_native_first", "bid": 2, "ct": 1, "lid": 1, "_match": 1},
                {"aid": "a_another_edition", "bid": 2, "ct": 1, "lid": 1, "_match": 1},
                {"aid": "foreign", "bid": 3, "ct": 1, "lid": 2, "_match": 1}]
        groups = audio_groups(rows, self.profile["playback"]["audio_selection"], language=1)
        self.assertEqual([row["aid"] for row in groups[0]], ["z_native_first", "a_another_edition"])
        self.assertEqual(len(groups), 1)

    def test_selected_audio_replaces_dash_default_and_retains_video(self):
        """最终计划只保留所选视频与独立音轨，内置低档音轨不冒充最高；无返回值。"""
        audio = {"url": "synthetic", "audio_asset": True}
        plan = {"format": "dash", "tracks": [{"kind": "audio"}, {"kind": "video"}],
                "segments": [{"track": 0}, {"track": 1}], "duration": 30}
        source = {"_audio_selection": {"native_selection_verified": True}, "_audio_segments": [audio]}
        with patch("qsv_local.playback.manifests.parse_media_manifest", return_value=plan):
            result = selected_media_plan(b"", source, self.profile)
        self.assertEqual(result["tracks"], [{"kind": "video"}])
        self.assertEqual(result["segments"], [{"track": 0}, audio])


if __name__ == "__main__":
    unittest.main()
