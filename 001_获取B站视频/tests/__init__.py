"""B 站工具测试包；通过 unittest 发现用例，共用 PROJECT_DIR 和 CONFIG_PATH 定位资源及主配置。"""

from pathlib import Path

from bilibili_downloader.auth import AuthSnapshot


PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / 'config' / 'config.yaml'


def fake_auth_snapshot() -> AuthSnapshot:
    """返回仅用于离线调度测试的认证快照；无参数，不包含真实凭据。"""
    return AuthSnapshot('BilibiliUnitTest', ({'name': 'SESSDATA', 'value': 'fake-only', 'domain': '.bilibili.com',
                                            'path': '/', 'secure': True, 'expires': -1, 'hostOnly': False},))
