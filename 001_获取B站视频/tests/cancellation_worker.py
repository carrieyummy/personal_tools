"""取消回归用的独立进程入口，只访问测试目录和本地 HTTP，不连接原站。"""

from pathlib import Path
import signal
import subprocess
import sys
from threading import enumerate as enumerate_threads
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataclasses import replace
from bilibili_downloader import application, auth, config, runtime, scheduler
from bilibili_downloader.errors import check_cancel
from tests import CONFIG_PATH, fake_auth_snapshot
import download_bilibili as cli


def main() -> int:
    """读取测试目录、场景和本地 URL，运行真实入口并记录剩余线程；返回 CLI 退出码，无参数。"""
    root, mode, url = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    settings = replace(config.load_settings(CONFIG_PATH), output_dir=root, threads=2, max_in_flight=1,
                       log_refresh_interval=0.1, cancel_poll_interval=0.05)

    def operation(args: object, settings: config.Settings, journal: object) -> int:
        """模拟 args 指定的下载，使用 settings/journal 走真实取消及资源管理；返回正常退出码。"""
        controller = runtime.current()
        activity = journal.add('下载' if mode == 'network' else '合并', '取消回归', status='downloading')

        def execute_job(job: int, event: object) -> int:
            """对 job 执行可取消 HTTP 或长媒体进程，event 由调度器传入；成功返回编号。"""
            if mode == 'network':
                with auth.worker_session(fake_auth_snapshot(), settings) as session:
                    if url.startswith('https:'):
                        # 只对隔离测试服务器的临时自签名证书关闭校验，生产下载器保持原配置。
                        session.downloader.params['nocheckcertificate'] = True
                    response = session.get(url)
                    try:
                        with (root / 'video.part').open('ab') as stream:
                            first = response.response.read(1024)
                            stream.write(first)
                            stream.flush()
                            (root / 'ready').write_text('ready', encoding='utf-8')
                            import faulthandler
                            faulthandler.dump_traceback_later(4, file=sys.stderr)
                            while chunk := response.response.read(1024):
                                check_cancel(event)
                                stream.write(chunk)
                                activity.progress({'downloaded_bytes': stream.tell()})
                    finally:
                        response.dispose()
            else:
                child_code = ("import os,time; from pathlib import Path; "
                              "Path(__import__('sys').argv[1]).write_text(str(os.getpid())); time.sleep(120)")
                runtime.run_process([sys.executable, '-c', child_code, str(root / 'media.pid')],
                                    capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            return job

        def execute(job: int, event: object) -> int:
            """将 job/event 传入当前取消上下文；返回任务结果。"""
            return runtime.run_bound(controller, execute_job, job, event)

        def prepare(job: int) -> int:
            """准备 job；自发 SIGINT 场景在第二项派发前发送真实 Python 信号，返回编号。"""
            if mode == 'sigint' and job == 2:
                import time
                deadline = time.monotonic() + 10
                while not (root / 'media.pid').exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                signal.raise_signal(signal.SIGINT)
                signal.raise_signal(signal.SIGINT)
            return job

        from collections import deque

        def download(job: int, event: object) -> tuple[int, int]:
            """对 job/event 执行网络或 SIGINT 下载场景；媒体场景留给后处理，返回编号及零队列字节。"""
            if mode != 'media':
                execute(job, event)
            return job, 0

        def process(value: tuple[int, int], event: object) -> int:
            """对 value/event 执行媒体后处理场景，其他模式直接返回编号。"""
            return execute(value[0], event) if mode == 'media' else value[0]

        scheduler.run_pipeline([1, 2] if mode == 'sigint' else [1], settings,
                               prepare, download, process, lambda *values: None, controller.event,
                               downloaded_size=lambda value: value[1], waiting=lambda *values: None,
                               finishers=deque())
        return 0

    with patch.object(config, 'load_settings', return_value=settings), patch.object(application, 'run_with_settings', side_effect=operation):
        code = cli.main(['--url', 'BV1xx411c7mD'])
    remaining = [thread.name for thread in enumerate_threads() if thread.name.startswith('bilibili')]
    (root / 'remaining_threads.json').write_text(__import__('json').dumps(remaining), encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
