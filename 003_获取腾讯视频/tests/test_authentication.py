"""隔离验证登录轮换、并发恢复及批次停止；不读取真实账号，不访问实网。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from threading import Barrier, Event, Thread
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs

from local.configuration import load_playback_config
from local.playback.application import UrlBatch
from local.playback.auth import LoginSession
from local.playback.client import NativeClient
from local.playback.errors import (AddressExpired, ApiHTTPError, AuthenticationError,
                                   AuthenticationRejected, AuthenticationStopped, PlaybackError)
from local.playback.transport import Transport
from local.runtime import TaskCancelled
from tests.test_batch import MemoryJournal
from tests.test_playback import current_config


def user(session: str = "old-session", account: str = "same-account") -> dict:
    """用 session 票据和 account 账号构造内存登录替身；返回不含真实登录信息的字典。"""
    return {"tokens": {"vuserid": account, "vusession": session, "main_login": "qq"}, "app_version": "test"}


class LoginSessionTests(unittest.TestCase):
    """用可控时钟和线程屏障复现长时间运行时的凭据轮换。"""

    def setUp(self):
        """读取当前配置并创建单调时钟替身，无参数或返回值。"""
        self.config = current_config()
        self.tick = 0.0
        self.loader = Mock(return_value=user("new-session"))
        self.session = LoginSession(self.config, user=user(), loader=self.loader, clock=lambda: self.tick)

    def test_periodic_snapshot_is_immutable_and_single_read(self):
        """到实际配置间隔才重读，旧快照不可修改且仍保留本次请求的身份。"""
        first = self.session.snapshot()
        self.assertEqual(self.session.snapshot(), first)
        self.loader.assert_not_called()
        with self.assertRaises(TypeError):
            first.tokens["vusession"] = "mutated"
        self.tick += self.config["authentication"]["refresh_interval_seconds"]
        second = self.session.snapshot()
        self.assertGreater(second.generation, first.generation)
        self.assertEqual(second.tokens["vusession"], "new-session")
        self.assertEqual(first.tokens["vusession"], "old-session")
        self.session.snapshot()
        self.loader.assert_called_once_with(self.config)

    def test_concurrent_old_rejections_share_one_refresh(self):
        """所有线程先取得旧快照，迟到拒绝必须复用已成功的新代次，不能再次读取或熔断。"""
        workers = 4
        barrier = Barrier(workers)
        def request(snapshot):
            """旧 snapshot 等待其他请求后拒绝，新代次返回成功字典。"""
            if snapshot.generation == 1:
                barrier.wait(timeout=5)
                raise AuthenticationRejected({"login": "0"})
            return {"generation": snapshot.generation}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(lambda _: self.session.execute(request), range(workers)))
        self.loader.assert_called_once()
        self.assertEqual({item["generation"] for item in results}, {2})
        self.assertEqual(self.session.diagnostics()["recoveries"], 1)
        self.assertFalse(self.session.stopped.is_set())

    def test_terminal_failure_is_bounded_and_shared(self):
        """并发拒绝只由一个线程耗尽配置重试次数，其余线程停止，不各自重新登录。"""
        barrier = Barrier(3)
        def rejected(snapshot):
            """让初始 snapshot 同步进入拒绝，后续所有代次同样明确拒绝。"""
            if snapshot.generation == 1:
                barrier.wait(timeout=5)
            raise AuthenticationRejected({"login": "0"})
        def outcome(_):
            """返回一次认证调用的异常类型，忽略映射输入 _；不接触异常票据。"""
            try:
                self.session.execute(rejected)
            except AuthenticationError as error:
                return type(error)
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(outcome, range(3)))
        self.assertEqual(results.count(AuthenticationError), 1)
        self.assertEqual(results.count(AuthenticationStopped), 2)
        self.assertEqual(self.loader.call_count, self.config["authentication"]["recovery_attempts"])
        self.assertTrue(self.session.stopped.is_set())

    def test_periodic_read_failure_stops_without_using_stale_tokens(self):
        """周期读取失败按配置有界重试，不继续用旧票据发请求，诊断不含原始错误。"""
        self.loader.side_effect = AuthenticationError("private-loader-detail")
        self.tick += self.config["authentication"]["refresh_interval_seconds"]
        request = Mock()
        with self.assertRaises(AuthenticationError) as caught:
            self.session.execute(request)
        request.assert_not_called()
        self.assertEqual(self.loader.call_count, self.config["authentication"]["recovery_attempts"])
        self.assertNotIn("private-loader-detail", str(caught.exception))
        self.assertEqual(self.session.diagnostics()["last_refresh"]["result"], "read_failed")

    def test_account_switch_cannot_continue_current_batch(self):
        """客户端切换账号后停止批次，不把另一个账号的票据用于原任务。"""
        self.loader.return_value = user("other-session", "other-account")
        self.tick += self.config["authentication"]["refresh_interval_seconds"]
        with self.assertRaisesRegex(AuthenticationError, "账号已切换"):
            self.session.snapshot()
        self.assertTrue(self.session.stopped.is_set())
        self.assertEqual(self.session.current.tokens["vuserid"], "same-account")

    def test_transport_failure_during_recovery_does_not_mean_logout(self):
        """已重读后遇到网络故障只使本次请求失败，不能永久熔断新登录态。"""
        def request(snapshot):
            """旧 snapshot 拒绝认证，新 snapshot 模拟网络故障；无正常返回。"""
            if snapshot.generation == 1:
                raise AuthenticationRejected({"login": "0"})
            raise PlaybackError("网络暂时不可用")
        with self.assertRaisesRegex(PlaybackError, "网络暂时"):
            self.session.execute(request)
        self.assertFalse(self.session.stopped.is_set())
        self.assertEqual(self.session.execute(lambda snapshot: {"ok": snapshot.generation}), {"ok": 2})

    def test_waiting_for_recovery_can_be_cancelled(self):
        """等待登录锁的线程响应批次取消，不需要等网络恢复请求结束。"""
        cancel, acquired, release = Event(), Event(), Event()
        self.session.cancel = cancel
        def hold():
            """持有登录锁直到 release；用 acquired 通知主线程，无返回值。"""
            with self.session.lock:
                acquired.set()
                release.wait(timeout=5)
        holder = Thread(target=hold)
        holder.start()
        self.assertTrue(acquired.wait(timeout=5))
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(self.session.snapshot)
                cancel.set()
                with self.assertRaises(TaskCancelled):
                    pending.result(timeout=5)
        finally:
            release.set()
            holder.join(timeout=5)


class ClientAuthenticationTests(unittest.TestCase):
    """校验认证恢复保留请求语义，并隔离 API/媒体错误。"""

    def setUp(self):
        """按当前专项配置创建内存登录客户端；所有签名与网络由各测试替换。"""
        self.config = current_config()
        self.client = NativeClient(self.config, user=user())
        self.settings = self.config["playback"]["playback"]

    def response(self, authenticated: bool = True) -> dict:
        """按 authenticated 构造成功或拒绝的最小协议响应，返回字典。"""
        return {"code": float(self.settings["success_codes"][0]),
                "login": self.settings["authenticated_states"][0] if authenticated else -1,
                "vl": {"vi": [{"vid": "video"}]}}

    def test_refresh_retries_same_video_definition_audio_with_new_signature(self):
        """一次认证拒绝后原 cid/vid/画质/音轨不变，Cookie 与 logintoken 同步使用新票据和新签名。"""
        self.client.authentication.loader = Mock(return_value=user("new-session"))
        calls = []
        def proxy(endpoint, headers, body, *, api):
            """解析 endpoint 的模拟代理请求 headers/body；api 必须标识接口，返回内层响应 JSON。"""
            query = parse_qs(json.loads(body)["vinfoparam"])
            tokens = json.loads(query["logintoken"][0])
            self.assertIn(f"vusession={tokens['vusession']}", headers["Cookie"])
            self.assertTrue(api)
            calls.append(query)
            return json.dumps({"vinfo": json.dumps(self.response(len(calls) > 1))})
        signed = [{"ckey": "first-key", "guid": "device"}, {"ckey": "second-key", "guid": "device"}]
        with patch("local.playback.client.native_key", side_effect=signed), \
             patch.object(self.client.transport, "text", side_effect=proxy):
            self.client.source("cover", "video", "chosen-definition", {"audio_track": "chosen-audio"})
        self.assertEqual(len(calls), 2)
        for key in ("cid", "vid", "defn", "audio_track"):
            self.assertEqual(calls[0][key], calls[1][key])
        self.assertNotEqual(calls[0]["cKey"], calls[1]["cKey"])
        self.assertNotEqual(calls[0]["logintoken"], calls[1]["logintoken"])
        self.assertFalse(self.client.authentication.stopped.is_set())

    def test_rotation_during_signing_cannot_mix_request_credentials(self):
        """签名期间另一请求刷新快照时，本次头和 JSON 仍使用同一旧快照。"""
        session = self.client.authentication
        tick = [session.current.loaded_at]
        session.clock = lambda: tick[0]
        session.loader = Mock(return_value=user("new-session"))
        def sign(*args, **kwargs):
            """模拟 args/kwargs 指定的签名期间发生周期刷新，返回合成签名。"""
            tick[0] += self.config["authentication"]["refresh_interval_seconds"]
            session.snapshot()
            return {"ckey": "key", "guid": "device"}
        with patch("local.playback.client.native_key", side_effect=sign):
            query, headers = self.client.signed_query("cover", "video", self.settings["query"])
        self.assertEqual(json.loads(query["logintoken"])["vusession"], "old-session")
        self.assertIn("vusession=old-session", headers["Cookie"])
        self.assertEqual(session.snapshot().tokens["vusession"], "new-session")

    def test_only_explicit_api_auth_http_triggers_recovery(self):
        """配置中的认证 HTTP 拒绝才触发重读；普通接口错误不使整个批次停止。"""
        for status in self.settings["authentication_http_codes"]:
            client = NativeClient(self.config, user=user())
            client.authentication.loader = Mock(return_value=user("new-session"))
            with patch.object(client, "raw_source", side_effect=[ApiHTTPError(status), self.response()]):
                self.assertEqual(client.source("cover", "video"), self.response())
            client.authentication.loader.assert_called_once()
        for status in set(self.settings["expired_http_codes"]) - set(self.settings["authentication_http_codes"]):
            with self.subTest(status=status), patch.object(self.client, "raw_source", side_effect=ApiHTTPError(status)):
                with self.assertRaises(ApiHTTPError):
                    self.client.source("cover", "video")
                self.assertFalse(self.client.authentication.stopped.is_set())
                self.assertEqual(self.client.authentication.diagnostics()["refreshes"], 0)

    def test_media_expiry_and_api_errors_have_different_types(self):
        """同一配置过期状态：媒体允许同源续签，接口保留 HTTP 状态而不冒充媒体过期。"""
        transport = Transport(self.config)
        for status in self.settings["expired_http_codes"]:
            for api, expected in ((False, AddressExpired), (True, ApiHTTPError)):
                opener = Mock()
                opener.open.side_effect = HTTPError("https://example.invalid/private", status, "failure", {}, None)
                with self.subTest(status=status, api=api), patch("local.playback.transport.build_opener", return_value=opener):
                    with self.assertRaises(expected) as caught:
                        transport.open("https://example.invalid/private?token=secret", api=api)
                self.assertNotIn("secret", str(caught.exception))

    def test_missing_login_is_protocol_error_not_global_logout(self):
        """响应缺少登录字段时无法确认授权，但不能直接将所有剧集判为登录失效。"""
        data = self.response()
        data.pop("login")
        with patch.object(self.client, "raw_source", return_value=data):
            with self.assertRaisesRegex(PlaybackError, "缺少登录状态"):
                self.client.source("cover", "video")
        self.assertFalse(self.client.authentication.stopped.is_set())

    def test_diagnostics_do_not_contain_tokens_or_signed_urls(self):
        """拒绝响应只保留脱敏白名单和代次，不保存票据、账号、设备或签名地址。"""
        data = self.response(False)
        data.update(em="vusession=private-session https://example.invalid/?secret=yes", exem="access_token=private-token same-account old-session new-session",
                    ignored_secret="private-extra")
        self.client.authentication.loader = Mock(return_value=user("new-session"))
        with patch.object(self.client, "raw_source", return_value=data):
            with self.assertRaises(AuthenticationError):
                self.client.source("cover", "video")
        text = json.dumps(self.client.authentication.diagnostics())
        for forbidden in ("private-session", "private-token", "private-extra", "same-account", "new-session", "old-session", "https://"):
            self.assertNotIn(forbidden, text)
        self.assertIn("last_rejection", text)

    def test_terminal_auth_failure_stops_dispatch_and_skips_remaining(self):
        """首次恢复失败记录一次失败，剩余任务不进入 job，不把未执行集数登记成接口失败。"""
        config = deepcopy(self.config)
        config["download"]["threads"] = 1  # 限制下载派发边界，避免本用例依赖线程完成次序。
        journal = MemoryJournal()
        batch = UrlBatch(config, journal, client=self.client)
        self.client.authentication.loader = Mock(return_value=user("new-session"))
        tasks = [{"series_id": "cover", "series": "测试", "video_id": f"video{i}", "title": f"第{i}集"} for i in range(5)]
        def resolve(client, task, selector, **kwargs):
            """通过 client 请求 task 的模拟拒绝响应；selector/kwargs 保留正式调用签名，无成功返回。"""
            return client.source(task["series_id"], task["video_id"])
        with patch.object(self.client, "raw_source", return_value=self.response(False)) as source, \
             patch("local.playback.application.resolve", side_effect=resolve), patch.object(batch, "_job_steps", wraps=batch._job_steps) as job:
            summary = batch.run(tasks)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped"], len(tasks) - 1)
        self.assertEqual(job.call_count, 1)
        self.assertEqual(source.call_count, 1 + config["authentication"]["recovery_attempts"])
        self.assertTrue(all(item["status"] == "skipped" and "未执行" in item["detail"] for item in journal.entries[1:]))

    def test_authentication_config_is_validated(self):
        """刷新间隔及恢复次数必须按 YAML 提供正整数，不读取硬编码默认值。"""
        for key in self.config["authentication"]:
            for value in (0, -1, True, 1.5, None):
                config = deepcopy(self.config)
                config["authentication"][key] = value
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, f"authentication.{key}"):
                    load_playback_config(config, config["config_path"].parent)

    def test_terminal_failure_skips_other_in_flight_tasks(self):
        """两个在途请求同时拒绝时只有恢复耗尽者失败，其他在途和未派发任务都保留为跳过。"""
        config = deepcopy(self.config)
        config["max_in_flight"] = config["download"]["threads"] = 2
        batch = UrlBatch(config, MemoryJournal(), client=self.client)
        self.client.authentication.loader = Mock(return_value=user("new-session"))
        barrier = Barrier(config["max_in_flight"])
        tasks = [{"series_id": "cover", "video_id": f"video{i}", "title": f"第{i}集"} for i in range(6)]
        def rejected(*args, **kwargs):
            """初始 args/kwargs 请求同步后拒绝，更新快照也拒绝；返回最小未认证响应。"""
            if kwargs["snapshot"].generation == 1:
                barrier.wait(timeout=5)
            return self.response(False)
        def resolved(client, task, selector, **kwargs):
            """使用 client 查询 task，selector/kwargs 保留正式接口；认证恢复失败会向上传播。"""
            return client.source(task["series_id"], task["video_id"])
        with patch.object(self.client, "raw_source", side_effect=rejected), \
             patch("local.playback.application.resolve", side_effect=resolved):
            summary = batch.run(tasks)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped"], len(tasks) - 1)
        self.assertEqual(self.client.authentication.loader.call_count, config["authentication"]["recovery_attempts"])


if __name__ == "__main__":
    unittest.main()
