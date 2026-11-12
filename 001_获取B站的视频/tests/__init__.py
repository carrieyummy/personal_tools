"""B 站工具测试包；通过 unittest 从项目根目录发现用例，共用 PROJECT_DIR 定位配置和扩展资源。"""

from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
