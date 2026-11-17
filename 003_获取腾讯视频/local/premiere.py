"""获取腾讯整剧首次上映日期，并为缓存任务复用同一专辑公开资料。"""

import re

from .playback.client import public_catalog_page
from .playback.tasks import cards, premiere_date
from .playback.transport import Transport, safe_message


def album_date(config: dict, series_id: str, video_id: str, expected_series: str | None = None) -> tuple[str, str]:
    """用 config 查询 series_id/video_id 的公开整剧资料；expected_series 可核对规范化剧名，返回源站名称和首次上映日期。"""
    profile = config["playback"]
    page = public_catalog_page(profile, Transport(config), series_id, video_id)
    intros = cards(page, profile["catalog"]["introduction_card"])
    if len(intros) != 1 or not isinstance(intros[0].get("params", {}).get("mod_title"), str):
        raise ValueError("腾讯整剧资料缺少唯一剧名")
    title = intros[0]["params"]["mod_title"]
    cleaned = re.sub(r'[\s<>:"/\\|?*\x00-\x1f]+', "_", title).strip(". _")
    if expected_series is not None and cleaned != expected_series:
        raise ValueError("腾讯整剧资料与缓存剧名不一致")
    return title, premiere_date(page, profile["catalog"], series_id, title)


def add_cache_dates(config: dict, tasks: list[dict]) -> tuple[list[dict], list[dict]]:
    """按 config 为缓存 tasks 每剧查询一次日期；返回可处理任务和逐集错误，不使用本地文件时间猜测。"""
    dates, errors, accepted, skipped = {}, {}, [], []
    for task in tasks:
        key = task["series_id"]
        if key not in dates and key not in errors:
            try:
                dates[key] = album_date(config, key, task["video_id"], task["series"])
            except Exception as error:
                errors[key] = safe_message(error)
        if key in errors:
            skipped.append({"video_id": task["video_id"], "title": task["title"], "status": "skipped", "detail": errors[key]})
        else:
            title, day = dates[key]
            clean_title = re.sub(r'[\s<>:"/\\|?*\x00-\x1f]+', "_", title).strip(". _")
            if clean_title != task["series"]:
                skipped.append({"video_id": task["video_id"], "title": task["title"], "status": "skipped", "detail": "同一整剧编号对应不同剧名"})
            else:
                accepted.append({**task, "release_date": day})
    return accepted, skipped
