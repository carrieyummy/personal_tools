"""隔离验证 URL 模式、分页选集、逐集画质和音质选择；不读取真实账号，不访问实网。"""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

from local.configuration import load_config, load_playback_config
from local.playback.audio import AudioOption, AudioSelector, options
from local.playback.client import formats
from local.playback.errors import PlaybackError, SourceChanged
from local.playback.hls import parse_media, playlists
from local.playback.source import resolve, stable_media
from local.playback.tasks import Request, catalog, normalize_url, requests


def current_config() -> dict:
    """读取当前项目 YAML 的 URL 配置；返回独立对象，不读取登录态或创建输出目录。"""
    return deepcopy(load_config(Path(__file__).resolve().parents[1] / "configs/config.yaml", source="url"))


class PlaybackConfigTests(unittest.TestCase):
    """确认来源分支、清单继承和链接验证均以当前配置为基准。"""

    def test_url_mode_does_not_require_cache(self):
        """URL 配置移除缓存字段后仍可加载；专项路径使用当前配置的已解析位置。"""
        original = Path(__file__).resolve().parents[1] / "configs/config.yaml"
        raw = yaml.safe_load(original.read_text(encoding="utf-8"))
        config = current_config()
        for key in ("input_root", "download_record", "native_profile", "selection"):
            raw.pop(key)
        for key in ("playback_profile", "default_list", "output_path", "work_dir", "ffmpeg_path"):
            raw[key] = str(config[key])
        raw["source"] = "url"
        with tempfile.TemporaryDirectory(prefix="tencent_config_") as folder:
            path = Path(folder) / "config.yaml"
            path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            self.assertEqual(load_config(path)["source"], "url")
            raw["source"] = "invalid"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)

    def test_list_inheritance_and_conflicting_types(self):
        """简单链接列表共用全局设置；错误类型及旧对象格式被拒绝。"""
        config = current_config()
        with tempfile.TemporaryDirectory(prefix="tencent_list_") as folder:
            config["default_list"] = Path(folder) / "list.yaml"
            listing = {"date": "备注", "quality": config["download"]["quality"] + 1,
                       "all-parts": not config["download"]["all_parts"], "download_list": [
                           "https://m.v.qq.com/x/m/play?cid=cover&vid=video",
                           "https://v.qq.com/x/cover/cover/video.html"]}
            config["default_list"].write_text(yaml.safe_dump(listing), encoding="utf-8")
            values = requests(config)
            self.assertEqual(values[0].quality, listing["quality"])
            self.assertEqual(values[0].all_parts, listing["all-parts"])
            self.assertEqual(values[1].quality, listing["quality"])
            listing["quality"] = True
            config["default_list"].write_text(yaml.safe_dump(listing), encoding="utf-8")
            with self.assertRaises(PlaybackError):
                requests(config)
            listing["quality"] = config["download"]["quality"]
            for invalid in ({"url": "https://v.qq.com/x/page/video.html"}, "", " https://v.qq.com/x/page/video.html"):
                listing["download_list"] = [invalid]
                config["default_list"].write_text(yaml.safe_dump(listing), encoding="utf-8")
                with self.assertRaises(PlaybackError):
                    requests(config)

    def test_official_links_only(self):
        """移动链接、桌面正片和整剧地址均解析稳定编号；伪官方域名和路径穿越被拒绝。"""
        self.assertEqual(normalize_url("https://m.v.qq.com/x/m/play?cid=abc&vid=def"), ("abc", "def"))
        self.assertEqual(normalize_url("https://v.qq.com/x/cover/abc/def.html"), ("abc", "def"))
        self.assertEqual(normalize_url("https://v.qq.com/x/cover/abc.html"), ("abc", ""))
        for value in ("https://v.qq.com.evil.test/x/cover/abc.html", "file:///x/page/abc.html",
                      "https://m.v.qq.com/x/m/play?cid=../bad&vid=abc"):
            with self.subTest(value=value), self.assertRaises(PlaybackError):
                normalize_url(value)

    def test_audio_quality_requires_positive_integer(self):
        """音质排名必须是正整数；错误类型和缺失配置在启动校验时被拒绝，不使用代码默认值。"""
        for value in (0, -1, True, 1.5, "1", None):
            config = current_config()
            config["audio"]["quality"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "audio.quality"):
                load_playback_config(config, config["config_path"].parent)


class CatalogTests(unittest.TestCase):
    """用当前专项配置的模块类型构造分页，检查完整性和正片筛选。"""

    def make_page(self, numbers: list[int], *, first: bool = False) -> dict:
        """以 numbers 集序构造源站页面；first 决定是否包含简介和后续分组，返回原生页面结构。"""
        import json
        settings = self.config["playback"]["catalog"]
        rows = [{"params": {"title": str(number), "is_trailer": "0", "cid": "cover",
                             "vid": f"video{number}", "play_title": f"正片{number}"}} for number in numbers]
        rows.append({"params": {"title": "速通版", "is_trailer": "0"}})
        rows.append({"params": {"title": "99", "is_trailer": "1"}})
        module = {"type": settings["episode_card"], "params": {"tabs": json.dumps([
            {"selected": True, "page_context": "first"}, {"selected": False, "page_context": "second"}]) if first else "[]",
            "page_id": "episodes", "has_next": "false"}, "children_list": {"0": {"cards": rows}}}
        cards = [module]
        if first:
            cards.append({"type": settings["introduction_card"], "params": {"mod_title": "源站剧名"},
                          "children_list": {"0": {"cards": [{"params": {"title": "源站剧名", "report.cid": "cover", "broadcast_time": "2020-02-03"}}]}}})
        return {"CardList": cards}

    def setUp(self):
        """加载当前配置，无账号和网络访问；无参数或返回值。"""
        self.config = current_config()

    def test_tabs_are_loaded_even_if_initial_has_no_next(self):
        """首分组 has_next=false 不代表整剧结束；加载后续 tab、按数字集序排序且排除预告。"""
        client = SimpleNamespace(profile=self.config["playback"], catalog_page=Mock(side_effect=[
            self.make_page([2, 1], first=True), self.make_page([3])]))
        request = Request("cover", "video2", self.config["download"]["quality"], True, "备注")
        tasks = catalog(client, request)
        self.assertEqual([task["index"] for task in tasks], [1, 2, 3])
        self.assertEqual([task["page_url"] for task in tasks],
                         [f"https://v.qq.com/x/cover/cover/video{index}.html" for index in (1, 2, 3)])
        client.catalog_page = Mock(side_effect=[self.make_page([2, 1], first=True), self.make_page([3])])
        self.assertEqual([task["video_id"] for task in catalog(client, replace(request, all_parts=False))], ["video2"])

    def test_duplicate_page_is_rejected(self):
        """重复分组不能被当作成功结束，否则会漏集；无参数或返回值。"""
        client = SimpleNamespace(profile=self.config["playback"], catalog_page=Mock(side_effect=[
            self.make_page([1], first=True), self.make_page([1])]))
        with self.assertRaises(PlaybackError):
            catalog(client, Request("cover", "video1", 1, True, ""))

    def test_empty_tabs_still_follows_pagination(self):
        """腾讯返回空分组时仍读取下一页，确保不会遗漏正片。"""
        first = self.make_page([1], first=True)
        params = first["CardList"][0]["params"]
        params.update(tabs="", has_next="true", next_page_context="next")
        client = SimpleNamespace(profile=self.config["playback"], catalog_page=Mock(side_effect=[
            first, self.make_page([2])]))
        request = Request("cover", "video1", self.config["download"]["quality"], True, "")
        self.assertEqual([task["index"] for task in catalog(client, request)], [1, 2])
        self.assertEqual(client.catalog_page.call_count, 2)

    def test_invalid_tabs_are_not_treated_as_empty(self):
        """非空但格式错误的分组数据必须失败，避免静默遗漏分组。"""
        for value in ("invalid", "null", "{}"):
            first = self.make_page([1], first=True)
            first["CardList"][0]["params"]["tabs"] = value
            client = SimpleNamespace(profile=self.config["playback"], catalog_page=Mock(return_value=first))
            with self.subTest(value=value), self.assertRaises(ValueError):
                catalog(client, Request("cover", "video1", self.config["download"]["quality"], True, ""))


class AudioTests(unittest.TestCase):
    """验证按配置选择可下载音质、排除滤镜、逐集排名和实际复用原轨结构。"""

    def setUp(self):
        """读取当前 audio 配置，用合成音质选项隔离客户端；无返回值。"""
        self.settings = current_config()["audio"]
        self.standard = AudioOption("standard", "标准", False, True, 0, "original")
        self.vip = AudioOption("vip", "VIP原轨", True, True, 1, "track")

    def ranked_options(self, count: int) -> list[AudioOption]:
        """构造 count 个按等级递增的合成可下载轨，混合会员标记；返回独立选项列表，不读取真实账号。"""
        return [replace(self.standard, id=f"audio{index}", name=f"音源{index}", rank=index,
                        kind="track", vip=index % 2 == 0) for index in range(count)]

    def test_configured_quality_uses_only_downloadable_sources(self):
        """依据当前排名配置选择音源；未授权、不可下载或实时滤镜不占排名，也不读取终端输入。"""
        values = self.ranked_options(max(self.settings["quality"] + 2, 3))
        excluded = [replace(self.vip, id="locked", rank=len(values) + 1, authorized=False),
                    replace(self.vip, id="unavailable", rank=len(values) + 2, downloadable=False),
                    replace(self.vip, id="filter", rank=len(values) + 3, kind="effect")]
        with patch("builtins.input", side_effect=AssertionError("不能要求用户选择滤镜")):
            selected = AudioSelector(self.settings).select([*excluded, *values])
        self.assertIs(selected, values[-self.settings["quality"]])

    def test_native_selected_muxed_track_and_vip_effect_are_distinct(self):
        """原生 al.ai 的已选复用轨保留真实名称和规格；afl VIP 实时滤镜不进入下载音质列表。"""
        selected = {"id": "synthetic-format", "audiobandwidth": 12345}
        value = {"id": selected["id"], "name": "源站标准", "type": 1, "sl": 1, "lmt": 0, "preview": -1,
                 "fs": 0, "control": 0, "feature": 0, "action": "", "filename": "", "keyid": "", "ul": {"ui": []},
                 "audioscore": 80.5, "audio": 7, "track": "pcg2.0"}
        data = {"al": {"ai": [value]}, "afl": {"afi": [{"name": "clear_voice", "cname": "清澈人声",
                                                        "lmt": 1, "audiofilterparam": "synthetic-filter"}]}}
        values = options(data, {}, selected)
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].name, value["name"])
        self.assertEqual(values[0].metadata["track"], value["track"])
        self.assertTrue(values[0].downloadable)
        value["ul"]["ui"] = [{"url": "https://media.invalid/private"}]
        with self.assertRaises(PlaybackError):
            options(data, {}, selected)

    def test_insufficient_downloadable_quality_is_rejected(self):
        """实际可下载档位少于当前配置时明确失败；增加未授权音源或滤镜不能补足数量。"""
        values = self.ranked_options(self.settings["quality"] - 1)
        values += [replace(self.vip, authorized=False), replace(self.vip, id="effect", kind="effect")]
        with self.assertRaises(PlaybackError):
            AudioSelector(self.settings).select(values)

    def test_each_episode_uses_current_quality_ranking(self):
        """同一选择器按本次音源等级选择，评分或选项变化后不缓存旧档位。"""
        values = self.ranked_options(self.settings["quality"] + 1)
        selector = AudioSelector(self.settings)
        self.assertIs(selector.select(values), values[-self.settings["quality"]])
        refreshed = [replace(values[0], rank=len(values)), *values[1:]]
        expected = sorted(refreshed, key=lambda item: item.rank, reverse=True)[self.settings["quality"] - 1]
        self.assertIs(selector.select(refreshed), expected)

    def test_unknown_quality_ranking_is_rejected(self):
        """多个可下载音源缺少有限数值等级时不能猜测最高音质，也不退回交互选择。"""
        values = self.ranked_options(max(self.settings["quality"], 2))
        for rank in (None, float("nan"), True):
            with self.subTest(rank=rank), self.assertRaises(PlaybackError):
                AudioSelector(self.settings).select([replace(values[0], rank=rank), *values[1:]])

    def test_runtime_effect_not_mislabelled_as_audio(self):
        """原始音源不因标准效果重复计档；多个 VIP 滤镜均不进入下载音质列表。"""
        data = {"afl": {"afi": [
            {"name": "standard", "show_name": "标准", "audiofilterparam": "", "vip_limit": 0},
            {"name": "voice", "show_name": "清澈人声", "audiofilterparam": "runtime-filter", "vip_limit": 1},
            {"name": "surround", "show_name": "立体环绕", "audiofilterparam": "other-filter", "vip_limit": 1}]}}
        values = options(data, {}, {"audiobandwidth": 1000})
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].kind, "original")
        self.assertEqual(values[0].metadata["bitrate"], 1000)

    def test_unknown_separate_audio_is_not_silently_ignored(self):
        """公开播放器 al.ai 是独立音轨入口；协议未验证时须明确失败，不能静默只下载标准轨。"""
        with self.assertRaises(PlaybackError):
            options({"al": {"ai": [{"keyid": "audio", "name": "另一个原始音源"}]}}, {}, {})


class SourceTests(unittest.TestCase):
    """检查数值画质排名、服务端降档、来源刷新和清单语义指纹。"""

    def setUp(self):
        """以当前配置和合成两档来源创建无网络客户端；无参数或返回值。"""
        self.config = current_config()
        self.qualities = [dict(id=1, name="lower", sname="普通", width=96, height=64, vfps=12,
                               videoenh=1, zqe=1, bandwidth=1000, audiobandwidth=1000),
                          dict(id=2, name="higher", sname="高清", width=96, height=64, vfps=24,
                               videoenh=1, zqe=2, bandwidth=2000, audiobandwidth=2000)]
        self.task = dict(series_id="cover", video_id="video", quality=1)
        self.selector = AudioSelector(self.config["audio"])
        self.client = SimpleNamespace(config=self.config, profile=self.config["playback"],
                                      transport=Mock(), source=self.response)

    def response(self, cid, vid, definition=None, extra=None):
        """为 cid/vid/definition 返回合成授权响应，extra 为预留请求参数；返回含内嵌清单的原生字典。"""
        selected = self.qualities[-1] if definition is None else next(item for item in self.qualities if item["name"] == definition)
        return {"fl": {"fi": [{**item, "sl": int(item["id"] == selected["id"])} for item in self.qualities]},
                "vl": {"vi": [{"vid": vid, "st": self.config["playback"]["playback"]["full_video_states"][0],
                    "fn": f"file{selected['id']}", "fmd5": "mediahash", "td": "2", "vw": selected["width"],
                    "vh": selected["height"], "fps": selected["vfps"],
                    "ul": {"ui": [{"url": "https://media.invalid/list.m3u8"}],
                           "m3u8": "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\na.ts?index=0&brs=0&bre=99&vkey=secret\n#EXT-X-ENDLIST\n"}}]}}

    def test_highest_fps_selected_and_refresh_identity(self):
        """尺寸相同仍应选更高帧率；同源续签身份一致，文件版本变化时拒绝旧断点。"""
        source = resolve(self.client, self.task, self.selector)
        self.assertEqual(source.description["format"]["id"], self.qualities[-1]["id"])
        self.assertEqual(resolve(self.client, self.task, self.selector, expected=source).identity, source.identity)
        original = self.response
        def changed(*args, **kwargs):
            """改变原生文件摘要，模拟服务器替换同集内容；参数转交原始替身，返回新响应。"""
            result = original(*args, **kwargs)
            result["vl"]["vi"][0]["fmd5"] = "newhash"
            return result
        self.client.source = changed
        with self.assertRaises(SourceChanged):
            resolve(self.client, self.task, self.selector, expected=source)

    def test_server_downgrade_is_rejected(self):
        """指定高档却返回低档时不能记为成功或重新编号。"""
        self.client.source = lambda cid, vid, definition=None, extra=None: self.response(cid, vid, "lower")
        with self.assertRaises(PlaybackError):
            resolve(self.client, self.task, self.selector)

    def test_preview_is_not_counted_as_authorized_quality(self):
        """即使高档尺寸与清单完整，显式试玩标记也不可算作账号可下载的完整档位。"""
        original = self.response
        def preview(cid, vid, definition=None, extra=None):
            """为高档响应附加试玩时长；其余 cid/vid/definition/extra 转交原替身，返回合成响应。"""
            result = original(cid, vid, definition, extra)
            result["preview"] = 1 if definition == self.qualities[-1]["name"] else 0
            result["exem"] = int(definition == self.qualities[-1]["name"])
            return result
        self.client.source = preview
        self.assertEqual(resolve(self.client, self.task, self.selector).description["format"]["id"], self.qualities[0]["id"])

    def test_preset_preview_duration_does_not_reject_full_video(self):
        """正常正片也会返回预设试玩时长，exem=0 时不能仅因 preview>0 误判为无权限。"""
        original = self.response
        def full(*args, **kwargs):
            """给原替身的参数 args/kwargs 对应正片附加预设试玩时长；返回完整授权响应。"""
            result = original(*args, **kwargs)
            result.update(preview=100, exem=0)
            return result
        self.client.source = full
        self.assertEqual(resolve(self.client, self.task, self.selector).description["format"]["id"], self.qualities[-1]["id"])

    def test_identical_stream_aliases_are_one_quality(self):
        """同一编码/尺寸/帧率的物理编号只排名一次，保留别名；编号属性冲突仍拒绝。"""
        standard = {**self.qualities[0], "formatdefn": "lower"}
        alias = {**standard, "name": "enhanced", "sname": "营销增强"}
        values = formats({"fl": {"fi": [alias, standard, self.qualities[1]]}}, self.config["playback"]["playback"])
        self.assertEqual(len(values), 2)
        self.assertEqual(values[-1]["name"], standard["name"])
        self.assertEqual(len(values[-1]["aliases"]), 2)
        alias["height"] += 1
        with self.assertRaises(PlaybackError):
            formats({"fl": {"fi": [standard, alias]}}, self.config["playback"]["playback"])

    def test_playlist_identity_keeps_ranges_and_ignores_signature(self):
        """签名变化不改变断点身份，腾讯 index/brs/bre 变化必须改变身份。"""
        text = self.response("cover", "video")["vl"]["vi"][0]["ul"]["m3u8"]
        first = parse_media(text, "https://media.invalid/list.m3u8")
        self.assertEqual(first.identity, parse_media(text.replace("secret", "refreshed"), "https://media.invalid/list.m3u8").identity)
        self.assertNotEqual(first.identity, parse_media(text.replace("bre=99", "bre=199"), "https://media.invalid/list.m3u8").identity)

    def test_query_ranges_have_exact_expected_size(self):
        """腾讯查询范围给出精确传输长度，非法或不成对范围被拒绝，签名不参与长度计算。"""
        text = self.response("cover", "video")["vl"]["vi"][0]["ul"]["m3u8"]
        self.assertEqual(parse_media(text, "https://media.invalid/list.m3u8").resources[0].expected_bytes, 100)
        for changed in (text.replace("brs=0", "brs=200"), text.replace("&bre=99", "")):
            with self.assertRaises(PlaybackError):
                _ = parse_media(changed, "https://media.invalid/list.m3u8").resources[0].expected_bytes

    def test_signed_cdn_path_does_not_break_same_file_resume(self):
        """腾讯授权目录会随续签变化；已核对同文件和范围应复用，物理段号或文件版本变化须隔离。"""
        filename = "source.f123.ts"
        text = ("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n"
                "00_source.f123.1.ts?index=0&brs=0&bre=99&vkey=secret\n#EXTINF:2,\n"
                "010_source.f123.1.ts?index=10&brs=100&bre=199&vkey=secret\n#EXT-X-ENDLIST\n")
        first = stable_media([parse_media(text, "https://media.invalid/tokenA/anotherA/list.m3u8")], filename)[0]
        second = stable_media([parse_media(text, "https://other.invalid/tokenB/anotherB/list.m3u8")], filename)[0]
        self.assertEqual(first.identity, second.identity)
        self.assertTrue(all(resource.stable_path for resource in second.resources))
        changed = stable_media([parse_media(text.replace("f123.1", "f123.2"), "https://media.invalid/tokenB/list.m3u8")], filename)[0]
        self.assertNotEqual(first.identity, changed.identity)
        other = stable_media([parse_media(text, "https://media.invalid/tokenA/list.m3u8")], "unrelated.ts")[0]
        self.assertIsNone(other.resources[0].stable_path)

    def test_master_selection_keeps_selected_frame_rate(self):
        """同尺寸主清单中，高码率低帧率变体不能替代用户指定的高帧率档。"""
        expected = self.qualities[-1]
        low, high = self.qualities[0]["vfps"], expected["vfps"]
        master = (f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=10000,RESOLUTION=96x64,FRAME-RATE={low}\nlow.m3u8\n"
                  f"#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=96x64,FRAME-RATE={high}\nhigh.m3u8\n")
        media = self.response("cover", "video")["vl"]["vi"][0]["ul"]["m3u8"]
        with patch("local.playback.hls.fetch_text", return_value=media) as fetch:
            playlists(SimpleNamespace(config=self.config), "https://media.invalid/master.m3u8", text=master, expected=expected)
        self.assertEqual(fetch.call_args.args[1], "https://media.invalid/high.m3u8")
