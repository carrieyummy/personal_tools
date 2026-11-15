"""使用临时合成视频项预览实时日志；所有预览记录退出时清理，不操作用户媒体。"""

from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.activity_log import Journal
from qsv_local.discovery import discover
from tests.support import ProjectCase


def main() -> int:
    """启动仅供界面检查的临时日志服务；循环展示合成进度，Ctrl+C 后关闭并清理，返回 0。"""
    fixture = ProjectCase()
    fixture.setUp()
    try:
        for name in ("剧集甲/第1集.qsv", "剧集甲/第2集.qsv", "剧集乙/授权失败.qsv", "剧集乙/转换中.qsv"):
            fixture.source(name)
        tasks = discover(fixture.config)
        with Journal(fixture.config) as history:
            entry = history.add(tasks[0])
            history.update(entry, status="completed", detail="上次运行的完成记录")
        with Journal(fixture.config) as journal:
            entries = [journal.add(task) for task in tasks]
            journal.update(entries[0], status="completed", detail="全部验证通过")
            journal.update(entries[1], status="reused", detail="源内容及成品校验匹配")
            journal.update(entries[2], "authorizing", status="failed", detail="合成失败样例：授权已过期")
            print(f"PREVIEW_URL={journal.url}", flush=True)
            try:
                while True:
                    journal.update(entries[3], "extracting", progress=(time.monotonic() % 100),
                                   detail="合成进度，仅用于界面测试")
                    time.sleep(fixture.config["logging"]["progress_interval_seconds"])
            except KeyboardInterrupt:
                return 0
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    raise SystemExit(main())
