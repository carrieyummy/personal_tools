"""验证链接下载网页按单集记录阶段并与旧 QSV 历史隔离。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
from queue import Queue
import tempfile
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

from qsv_local.activity_log import Journal
from qsv_local.playback.application import run_downloads
from qsv_local.playback.coordination import StageControls
from qsv_local.storage import atomic_json


class PlaybackJournalTests(unittest.TestCase):
    """使用实际日志配置副本和临时路径，验证链接记录与阶段转换。"""

    def setUp(self):
        """加载当前主配置并建立临时成品、现场目录；无返回值。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = yaml.safe_load((Path(__file__).resolve().parents[1] / "configs/config.yaml").read_text(encoding="utf-8"))
        self.config.update(output_path=root / "output", work_dir=root / "work")
        self.config["output_path"].mkdir()
        self.config["work_dir"].mkdir()
        self.config["logging"]["port"] = 0
        self.video = {"album_id": "123", "title": "测试剧", "episode": 1, "episode_title": "测试剧第1集", "tvid": "456"}

    def test_episode_download_and_processing_are_independent(self):
        """下载结束后解析进度独立计量，另一集失败不改变本集；无返回值。"""
        with Journal(self.config, mode="playback") as journal:
            first = journal.add_episode(self.video, 1, self.config["audio"]["quality"])
            other = journal.add_episode(dict(self.video, episode=2, tvid="457"), 1, self.config["audio"]["quality"])
            journal.update(first, "downloading", progress=50, current_bytes=50, total_bytes=100)
            journal.update(first, "decrypting", progress=20)
            journal.update(other, "resolving_source", status="failed", error="合成解析失败")
            data = journal.snapshot(live=False)["runs"][-1]["entries"]
            self.assertEqual(data[0]["download"]["progress"], 100)
            self.assertEqual(data[0]["processing"]["progress"], 20)
            self.assertEqual(data[1]["status"], "failed")
            self.assertEqual(data[0]["directory"], self.video["album_id"])
            self.assertEqual(data[0]["series_title"], self.video["title"])
            page = journal.render(live=False)
            self.assertIn("下载进度", page)
            self.assertIn("解析与保存进度", page)
            self.assertNotIn("__DATA__", page)
            self.assertFalse((self.config["output_path"] / self.config["logging"]["state_file"]).exists())

    def test_verification_device_is_retained_in_live_and_offline_state(self):
        """实际验证设备在阶段切换及完成后保留，未验证的历史条目不显示设备；无返回值。"""
        with Journal(self.config, mode="playback") as journal:
            entries = [journal.add_episode(dict(self.video, episode=index), 1, self.config["audio"]["quality"])
                       for index in (1, 2, 3)]
            for entry, device in zip(entries, ("cpu", "gpu")):
                journal.update(entry, "verifying", device=device)
                journal.update(entry, "checking")
                journal.update(entry, "completed", status="completed")
            for live in (True, False):
                data = journal.snapshot(live=live)["runs"][-1]["entries"]
                self.assertEqual([entry["processing"].get("device") for entry in data], ["cpu", "gpu", None])
                self.assertIn("验证设备：", journal.render(live=live))

    def test_episode_page_link_is_persisted_and_rendered(self):
        """当前集页面链接保存在状态和离线页面；历史记录缺少链接时仍可渲染；无返回值。"""
        profile = yaml.safe_load((Path(__file__).resolve().parents[1] / "configs/playback.yaml").read_text(encoding="utf-8"))
        url = "https://" + profile["network"]["allowed_hosts"][0] + "/?tvid=456"
        with Journal(self.config, mode="playback") as journal:
            journal.add_episode(dict(self.video, play_url=url), 1, self.config["audio"]["quality"])
            old = journal.add_episode(self.video, 1, self.config["audio"]["quality"])
            old.pop("play_url")
            for live in (True, False):
                self.assertEqual(journal.snapshot(live=live)["runs"][-1]["entries"][0]["play_url"], url)
                self.assertIn("页面链接：", journal.render(live=live))
        saved = json.loads(journal.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["runs"][-1]["entries"][0]["play_url"], url)

    def test_retry_last_failed_episode_through_live_page(self):
        """通过本机 API 重试最后一个失败集，并验证完成后由网页停止服务；无返回值。"""
        config = self.config
        config["logging"].update(open_browser=False)
        controls = StageControls(config)
        main = config["work_dir"] / "main.yaml"
        snapshot = {**config, "output_path": str(config["output_path"]), "work_dir": str(config["work_dir"])}
        main.write_text(yaml.safe_dump(snapshot, allow_unicode=True), encoding="utf-8")
        video = dict(self.video, index=0)
        identity = {"tvid": video["tvid"], "sha256": "a" * 64}
        completed = config["output_path"] / "existing.mp4"
        completed.write_bytes(b"published")
        other = config["work_dir"] / "playback_downloads" / ("457_" + "b" * 64)
        other.mkdir(parents=True)
        (other / "segment.ts").write_bytes(b"another episode")
        options = [{"index": 0, "url": "fixture", "quality": 1, "all_parts": True}]
        picked = SimpleNamespace(manifest=b"fixture", headers={}, report={"selection": {}}, selected={
            "code": 1, "dr": 0, "scrsz": "16x16", "_audio_selection": {"codec": "aac"}})
        urls, allow_failure, retry_started, release_retry = Queue(), Event(), Event(), Event()
        visits = []

        def journal_factory(*args, **kwargs):
            """用正式 args/kwargs 创建日志服务，传回本机 URL，返回日志对象。"""
            journal = Journal(*args, **kwargs)
            urls.put(journal.url)
            return journal

        def media_steps(profile, video, source, body, work, headers, selection, controls, notify):
            """用正式调用的 work/notify 模拟两次下载；其余参数不访问外网，第二次返回合成结果。"""
            visits.append(work)
            if len(visits) == 1:
                work.mkdir()
                atomic_json(work / "source_identity.json", identity)
                (work / "segment.ts").write_bytes(b"stale checkpoint")
                if not allow_failure.wait(5):
                    raise TimeoutError("测试网页未连接")
                notify({"stage": "validating_manifest", "detail": "校验下载清单"})
                raise ValueError("合成清单校验失败")
            self.assertFalse(work.exists())
            retry_started.set()
            if not release_retry.wait(5):
                raise TimeoutError("测试未释放重试任务")
            yield "download", 0
            return {"status": "completed", "output": str(completed)}

        def get_state():
            """读取 url 对应实时服务的 JSON 状态；返回独立快照。"""
            with urlopen(url + "api/state", timeout=5) as response:
                return json.load(response)

        def post_action(action: str, kind: str, target: str, token: str):
            """用 action/kind/target/token 提交本机控制请求；返回 HTTP 响应。"""
            return urlopen(Request(url + "api/" + action, data=json.dumps({"kind": kind, "id": target}).encode(),
                                   headers={"Content-Type": "application/json", "Origin": url.rstrip("/"),
                                            "X-Control-Token": token}), timeout=5)

        def wait_for_state(predicate):
            """轮询直到状态满足 predicate；返回匹配快照，超时令测试失败。"""
            deadline = monotonic() + 5
            while monotonic() < deadline:
                state = get_state()
                if predicate(state):
                    return state
                sleep(config["runtime"]["cancel_poll_interval_seconds"])
            self.fail("日志服务没有到达预期状态")

        def assert_rejected(action, kind, target, token, code):
            """验证 action/kind/target/token 组合被拒绝并返回 HTTP code；无返回值。"""
            with self.assertRaises(HTTPError) as rejected:
                post_action(action, kind, target, token)
            self.assertEqual(rejected.exception.code, code)
            rejected.exception.close()

        prefix = "qsv_local.playback.application."
        with (patch(prefix + "read_profile", return_value={"main_config": main}),
              patch(prefix + "media_config", return_value=snapshot), patch(prefix + "Journal", side_effect=journal_factory),
              patch(prefix + "StageControls", return_value=controls),
              patch(prefix + "task_options", return_value=options), patch(prefix + "linked_videos", return_value=[video]),
              patch(prefix + "expand_episodes", return_value=[video]), patch(prefix + "require_same_metadata"),
              patch(prefix + "select_av_source", return_value=picked) as select,
              patch(prefix + "selected_media_plan", return_value={"segments": []}),
              patch(prefix + "manifest_identity", return_value=identity), patch(prefix + "reusable_output", return_value=None) as reuse,
              patch(prefix + "download_steps", side_effect=media_steps), ThreadPoolExecutor(max_workers=1) as pool):
            future = pool.submit(run_downloads, main)
            url = urls.get(timeout=5)
            try:
                initial = get_state()
                run_id = initial["runs"][-1]["id"]
                token = initial["control_token"]
                assert_rejected("stop", "batch", run_id, token, 409)
                allow_failure.set()
                state = wait_for_state(lambda value: value["runs"][-1].get("awaiting_stop"))
                run = state["runs"][-1]
                self.assertTrue(state["retry_enabled"])
                row = run["entries"][0]
                self.assertEqual((row["status"], row["download"]["stage"]), ("failed", "validating_manifest"))
                assert_rejected("retry", "episode", row["id"], "invalid", 403)
                with post_action("retry", "episode", row["id"], token) as response:
                    self.assertEqual(response.status, 200)
                self.assertTrue(retry_started.wait(5))
                assert_rejected("retry", "episode", row["id"], token, 409)
                assert_rejected("stop", "batch", run_id, token, 409)
                release_retry.set()
                state = wait_for_state(lambda value: value["runs"][-1].get("awaiting_stop"))
                self.assertEqual(state["runs"][-1]["entries"][0]["status"], "completed")
                self.assertFalse(state["closed"])
                self.assertFalse(future.done())
                assert_rejected("stop", "batch", "old-run", token, 409)
                assert_rejected("stop", "batch", run_id, "invalid", 403)
                with post_action("stop", "batch", run_id, token) as response:
                    self.assertEqual(response.status, 200)
                state = wait_for_state(lambda value: value["closed"])
                with urlopen(url + "api/ack?revision=" + str(state["revision"]), timeout=5) as response:
                    self.assertEqual(response.status, 200)
                report, location = future.result(timeout=5)
            finally:
                allow_failure.set()
                release_retry.set()
                if not future.done():
                    controls.cancel.set()
        self.assertEqual(select.call_count, 2)
        reuse.assert_called_once()
        self.assertEqual((report["total_episodes"], report["completed"], report["failed"]), (1, 1, 0))
        self.assertEqual(report["results"][0]["retries"], 1)
        self.assertEqual(json.loads(location.read_text(encoding="utf-8"))["status"], "completed")
        self.assertEqual(completed.read_bytes(), b"published")
        self.assertEqual((other / "segment.ts").read_bytes(), b"another episode")


if __name__ == "__main__":
    unittest.main()
