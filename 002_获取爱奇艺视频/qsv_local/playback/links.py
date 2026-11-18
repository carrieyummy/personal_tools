"""读取链接下载清单，并验证简单 URL 列表。"""

import yaml


def read_link_listing(path) -> dict:
    """从 Path 类型的 path 读取 YAML；返回包含非空 URL 字符串列表的清单，无效时报错。"""
    listing = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    tasks = listing.get("download_list") if isinstance(listing, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("下载清单必须包含非空 download_list")
    for index, url in enumerate(tasks):
        if not isinstance(url, str) or not url.strip() or url != url.strip():
            raise ValueError(f"清单第 {index} 项必须是非空链接字符串，且首尾不能有空白")
    return listing
