"""用真实子进程验证各平台共享成品根目录；仅操作临时目录，不访问账号或媒体。"""

from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import tempfile
from threading import Thread
import unittest

import yaml


REPOSITORY = Path(__file__).resolve().parents[2]
PROJECTS = (
    ("002_获取爱奇艺视频", "qsv_local.storage"),
    ("003_获取腾讯视频", "local.storage"),
    ("004_获取优酷视频", "local.storage"),
    ("005_获取芒果视频", "local.storage"),
)
WORKER = '''"""按当前配置获取指定临时目录锁，通过标准输入控制释放。"""
from importlib import import_module
from pathlib import Path
import sys
import yaml

module = import_module(sys.argv[1])
lock_errors = (RuntimeError,)
if hasattr(module, "DirectoryLock"):
    lock_errors += (import_module("local.errors").ToolError,)
config = yaml.safe_load(Path("configs/config.yaml").read_text(encoding="utf-8-sig"))
name = sys.argv[3] if len(sys.argv) > 3 else config["logging"]["lock_file"]
root = Path(sys.argv[2])
try:
    lock = module.output_lock(root, name) if hasattr(module, "output_lock") else module.DirectoryLock(root / name)
    with lock:
        print("acquired", flush=True)
        sys.stdin.readline()
except ValueError:
    print("invalid", flush=True)
    sys.exit(24)
except lock_errors:
    print("locked", flush=True)
    sys.exit(23)
'''


class SharedOutputTests(unittest.TestCase):
    """配置派生的平台锁可并存，同平台锁互斥且崩溃后可重新取得。"""

    def setUp(self) -> None:
        """创建隔离根目录，读取各工具当前配置；无参数及返回值。"""
        temporary = tempfile.TemporaryDirectory(prefix="shared-output-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.configs = {
            name: yaml.safe_load((REPOSITORY / name / "configs/config.yaml").read_text(encoding="utf-8-sig"))
            for name, _ in PROJECTS
        }

    def start(self, project: tuple[str, str], root: Path, name: str | None = None) -> subprocess.Popen:
        """在 project 的模块环境启动锁进程；root 为临时目录，name 可覆盖配置以验证非法输入；返回进程。"""
        folder, module = project
        command = [sys.executable, "-c", WORKER, module, str(root)]
        if name is not None:
            command.append(name)
        child = subprocess.Popen(command, cwd=REPOSITORY / folder, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace")
        self.addCleanup(self.stop, child)
        return child

    def stop(self, child: subprocess.Popen) -> None:
        """回收 child 并关闭管道；异常路径先终止，避免测试失败遗留锁进程；无返回值。"""
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()

    def status(self, child: subprocess.Popen) -> str:
        """有界等待 child 的一行状态；无响应时测试失败，返回状态文本。"""
        messages = Queue()

        def read_status() -> None:
            """从当前子进程管道读一行并交给主线程；无参数及返回值。"""
            messages.put(child.stdout.readline().strip())

        Thread(target=read_status, daemon=True).start()
        try:
            return messages.get(timeout=15)
        except Empty:
            self.fail("锁进程未及时返回状态")

    def release(self, child: subprocess.Popen) -> None:
        """通知 child 正常退出锁上下文；检查退出码为零，无返回值。"""
        child.stdin.write("release\n")
        child.stdin.flush()
        self.assertEqual(child.wait(timeout=10), 0)

    def test_platforms_hold_the_same_root_without_conflict(self) -> None:
        """四个平台同时持有共享根目录各自的配置锁，根目录保留既有文件；无参数及返回值。"""
        names = [self.configs[folder]["logging"]["lock_file"] for folder, _ in PROJECTS]
        self.assertEqual(len({name.casefold() for name in names}), len(names), "共享根目录的平台锁名必须不同")
        existing = self.root / "existing.mp4"
        existing.write_bytes(b"existing media")
        children = [self.start(project, self.root) for project in PROJECTS]
        for child in children:
            self.assertEqual(self.status(child), "acquired")
        self.assertTrue(all(child.poll() is None for child in children))
        for name in names:
            self.assertTrue((self.root / name).is_file())
        for child in children:
            self.release(child)
        self.assertEqual(existing.read_bytes(), b"existing media")

    def test_same_platform_rejects_another_process_then_releases(self) -> None:
        """同平台第二进程被拒绝，正常退出后无需删除锁文件即可重获；无参数及返回值。"""
        for project in PROJECTS:
            with self.subTest(project=project[0]):
                first = self.start(project, self.root)
                self.assertEqual(self.status(first), "acquired")
                second = self.start(project, self.root)
                self.assertEqual(self.status(second), "locked")
                self.assertEqual(second.wait(timeout=10), 23)
                self.release(first)
                self.assertTrue((self.root / self.configs[project[0]]["logging"]["lock_file"]).exists())
                resumed = self.start(project, self.root)
                self.assertEqual(self.status(resumed), "acquired")
                self.release(resumed)

    def test_terminated_process_does_not_leave_a_stale_lock(self) -> None:
        """系统回收被终止进程的锁，锁文件保留也不妨碍续跑；无参数及返回值。"""
        for project in PROJECTS:
            with self.subTest(project=project[0]):
                first = self.start(project, self.root)
                self.assertEqual(self.status(first), "acquired")
                first.terminate()
                first.wait(timeout=10)
                resumed = self.start(project, self.root)
                self.assertEqual(self.status(resumed), "acquired")
                self.release(resumed)

    def test_work_and_output_locks_use_separate_directories(self) -> None:
        """相同平台锁名可同时用于独立输出和工作目录，工作目录仍拒绝其他写者；无参数及返回值。"""
        for project in PROJECTS[:3]:
            with self.subTest(project=project[0]):
                output = self.start(project, self.root / "output")
                work = self.start(project, self.root / "work")
                self.assertEqual(self.status(output), "acquired")
                self.assertEqual(self.status(work), "acquired")
                second = self.start(project, self.root / "work")
                self.assertEqual(self.status(second), "locked")
                self.assertEqual(second.wait(timeout=10), 23)
                self.release(output)
                self.release(work)

    def test_invalid_lock_names_cannot_escape_or_replace_records(self) -> None:
        """三平台拒绝越界、空白、保留设备名和非锁文件名，校验失败前不创建目录；无参数及返回值。"""
        for project in PROJECTS[:3]:
            for name in ("../escape.lock", "bad name.lock", "CON.lock", "video_info.json", ".lock"):
                with self.subTest(project=project[0], name=name):
                    root = self.root / "not-created"
                    child = self.start(project, root, name)
                    self.assertEqual(self.status(child), "invalid")
                    self.assertEqual(child.wait(timeout=10), 24)
                    self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
