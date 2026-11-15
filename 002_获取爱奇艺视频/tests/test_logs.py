"""验证实时日志 API、离线报告、持久化失败和文件访问边界。"""

from contextlib import redirect_stderr, redirect_stdout
import json
import re
import shutil
import subprocess
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

from qsv_local.activity_log import Journal
from qsv_local.discovery import discover
from qsv_local.storage import atomic_json, read_json
from tests.support import ProjectCase


class LogTests(ProjectCase):
    """使用真实回环 HTTP 服务验证内存日志与离线报告。"""

    def test_slow_log_write_does_not_block_state_updates(self):
        """慢写盘期间仍可更新内存，退出后最终状态必须落盘；无参数或返回值。"""
        self.source()
        started, release, updated = Event(), Event(), Event()
        with redirect_stdout(self.console), Journal(self.config) as journal:
            entry = journal.add(discover(self.config)[0])

            def slow_write(path, data):
                """等待 release 后将 data 原子写到 path，无返回值。"""
                started.set()
                if not release.wait(10):
                    raise TimeoutError("测试写盘协调超时")
                atomic_json(path, data)

            def update():
                """更新阶段后通知测试线程，无参数或返回值。"""
                journal.update(entry, "extracting", detail="继续处理")
                updated.set()

            with patch("qsv_local.activity_log.atomic_json", side_effect=slow_write):
                journal.update(entry, "reading")
                self.assertTrue(started.wait(10))
                thread = Thread(target=update)
                thread.start()
                try:
                    self.assertTrue(updated.wait(2), "日志慢写盘阻塞了状态更新")
                finally:
                    release.set()
                    thread.join(10)
        saved = read_json(self.output / self.config["logging"]["state_file"])
        self.assertEqual(saved["runs"][-1]["entries"][0]["stage"], "extracting")

    def test_live_state_and_files_do_not_read_persistent_json(self):
        """API 从内存读取，成品链接可访问，越界路径返回 404；无参数或返回值。"""
        self.source()
        task = discover(self.config)[0]
        task.target.parent.mkdir(parents=True)
        task.target.write_bytes(b"media")
        with redirect_stdout(self.console), Journal(self.config) as journal:
            entry = journal.add(task)
            journal.update(entry, "extracting", detail="处理中", output=str(task.target))
            with patch("qsv_local.activity_log.read_json", side_effect=AssertionError("不应读磁盘日志")):
                with urlopen(journal.url + "api/state") as response:
                    state = json.load(response)
                self.assertEqual(state["runs"][-1]["entries"][0]["status"], "running")
                with urlopen(journal.url + "files/series/episode.mp4") as response:
                    self.assertEqual(response.read(), b"media")
                with self.assertRaises(HTTPError) as error:
                    urlopen(journal.url + "files/../fixture.yaml")
                self.assertEqual(error.exception.code, 404)
                with urlopen(journal.url) as response:
                    self.assertIn("爱奇艺", response.read().decode("utf-8"))
            journal.update(entry, status="completed")
        saved = read_json(self.output / self.config["logging"]["state_file"])
        self.assertEqual(saved["runs"][-1]["status"], "completed")

    def test_export_is_standalone_and_preserves_older_snapshots(self):
        """同秒导出独立快照，不覆盖旧报告，并转义嵌入的脚本结束标记；无参数或返回值。"""
        self.source()
        with (redirect_stdout(self.console), patch("qsv_local.activity_log.time_tag", return_value="000101_000000"),
              Journal(self.config) as journal):
            entry = journal.add(discover(self.config)[0])
            journal.update(entry, "reading", detail="</script><script>alert(1)</script>")
            first, second = journal.export(), journal.export()
            self.assertNotEqual(first, second)
            before = first.read_bytes()
            html = second.read_text(encoding="utf-8")
            self.assertIn("const live=false", html)
            self.assertNotIn("</script><script>alert(1)</script>", html)
            with urlopen(journal.url + "export") as response:
                self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertEqual(first.read_bytes(), before)
        self.assertGreaterEqual(len(list(self.output.glob("*.html"))), 4)

    def test_log_write_failure_keeps_live_memory(self):
        """日志写盘失败不影响实时 API 和媒体状态；无参数或返回值。"""
        self.source()
        self.config["logging"]["write_retries"] = 0
        with (redirect_stdout(self.console), redirect_stderr(self.console),
              patch("qsv_local.activity_log.atomic_json", side_effect=OSError("模拟文件占用")),
              Journal(self.config) as journal):
            entry = journal.add(discover(self.config)[0])
            journal.update(entry, status="completed")
            with urlopen(journal.url + "api/state") as response:
                data = json.load(response)
            self.assertEqual(data["runs"][-1]["entries"][0]["status"], "completed")
            self.assertTrue(data["storage_error"])
        self.assertIn("转换继续", self.console.getvalue())

    def test_corrupt_history_is_preserved_before_start(self):
        """历史日志损坏时不覆盖未知内容也不启动处理；无参数或返回值。"""
        self.output.mkdir()
        history = self.output / self.config["logging"]["state_file"]
        history.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            Journal(self.config)
        self.assertEqual(history.read_text(encoding="utf-8"), "{broken")

    def test_interrupted_run_is_restored_as_interrupted(self):
        """上次未结束活动在新运行中标记中断并保留历史；无参数或返回值。"""
        self.source()
        with self.assertRaises(KeyboardInterrupt):
            with redirect_stdout(self.console), Journal(self.config) as journal:
                journal.update(journal.add(discover(self.config)[0]), "extracting")
                raise KeyboardInterrupt()
        saved = read_json(self.output / self.config["logging"]["state_file"])
        self.assertEqual(saved["runs"][-1]["entries"][0]["status"], "interrupted")
        with redirect_stdout(self.console), Journal(self.config) as again:
            self.assertEqual(again.data["runs"][0]["status"], "interrupted")
            self.assertEqual(len(again.data["runs"]), 2)

    def test_rendered_javascript_has_valid_syntax(self):
        """用已有 Node 检查实际渲染脚本语法；无参数或返回值，缺 Node 的环境跳过此附加检查。"""
        node = shutil.which("node")
        if not node:
            self.skipTest("未安装 Node")
        with redirect_stdout(self.console), Journal(self.config) as journal:
            html = journal.render(live=True)
        script = self.root / "rendered.js"
        script.write_text(re.search(r"<script>(.*?)</script>", html, re.S)[1], encoding="utf-8")
        result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
