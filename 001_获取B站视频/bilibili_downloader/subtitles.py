"""筛选平台字幕并独立保存原始 JSON 和 SRT 字幕。"""

from __future__ import annotations

import math
import re
from pathlib import Path
from threading import Event
from typing import Any

from . import api
from . import metadata as records
from . import naming
from .config import Settings
from .errors import DownloadCancelled, ToolError, check_cancel
from .models import SubtitleTrack, VideoTarget


def list_subtitles(context: Any, target: VideoTarget, info: dict[str, Any], settings: Settings) -> list[SubtitleTrack]:
    """查询 target/info 对应的字幕，按 settings 筛选语言与 AI；返回字幕列表，接口错误不伪装成无字幕。"""
    video_id = str(info["id"]).split("_p")[0]
    data = api.api_json(context, "/x/player/wbi/v2", settings, {"bvid": video_id, "cid": target.cid})["data"]
    if not isinstance(data, dict):
        raise ToolError("字幕列表格式异常")
    if data.get("need_login_subtitle"):
        raise ToolError("平台要求重新登录后才能获取字幕")
    tracks = []
    for item in data.get("subtitle", {}).get("subtitles", []):
        language = item.get("lan", "")
        url = item.get("subtitle_url", "")
        if not language or language == "danmaku" or not url:
            continue
        is_ai = language.startswith("ai-") or bool(item.get("ai_type"))
        if is_ai and not settings.include_ai:
            continue
        if "all" not in settings.subtitle_languages and not any(re.fullmatch(pattern, language) for pattern in settings.subtitle_languages):
            continue
        tracks.append(SubtitleTrack(language, item.get("lan_doc", language), is_ai, "https:" + url if url.startswith("//") else url, str(item.get("id", ""))))
    return tracks


def srt_timestamp(seconds: Any) -> str:
    """将非负、有限的 seconds 秒数转换为毫秒精度 SRT 时间文本；非法时间时报错。"""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        raise ToolError("字幕含非法时间戳")
    milliseconds = int(seconds * 1000 + 0.5)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def json_to_srt(data: dict[str, Any]) -> str:
    """把 B 站 data.body 字幕转换为 SRT 文本；保留文本与时间，不进行翻译或语音识别。"""
    body = data.get("body")
    if not isinstance(body, list):
        raise ToolError("字幕响应缺少 body 列表")
    entries = []
    for item in body:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise ToolError("字幕内容格式异常")
        start, end = item.get("from"), item.get("to")
        start_text, end_text = srt_timestamp(start), srt_timestamp(end)
        if end < start:
            raise ToolError("字幕结束时间早于开始时间")
        entries.append(f"{len(entries) + 1}\n{start_text} --> {end_text}\n{item['content']}\n")
    return "\n".join(entries)


def fetch_subtitles(context: Any, tracks: list[SubtitleTrack], folder: Path, stem: str, settings: Settings,
                    *, cancel_event: Event | None = None) -> list[str]:
    """逐条保存 tracks 到 folder/stem；context/settings 控制请求，cancel_event 中止后续请求，返回失败项。"""
    errors = []
    used_names: set[str] = set()
    for track in tracks:
        check_cancel(cancel_event)
        suffix = naming.safe_name(track.language, 30) + (".ai" if track.is_ai else "")
        if suffix in used_names:
            suffix += "." + naming.safe_name(track.track_id, 20)
        used_names.add(suffix)
        path = folder / f"{stem}.{suffix}.srt"
        if path.is_file() and path.stat().st_size:
            print(f"字幕已存在：{path.name}")
            continue
        try:
            for attempt in range(settings.retries + 1):
                check_cancel(cancel_event)
                response = None
                try:
                    response = context.request.get(track.url, headers={"Referer": api.HOME_URL}, timeout=settings.request_timeout * 1000)
                    if not response.ok:
                        raise ToolError(f"HTTP {response.status}")
                    text = json_to_srt(response.json())
                    break
                except Exception:
                    if attempt == settings.retries:
                        raise
                finally:
                    if response is not None:
                        response.dispose()
            records.atomic_text(path, text)
            print(f"字幕已保存：{path.name}")
        except DownloadCancelled:
            raise
        except Exception as exc:
            errors.append(f"字幕 {track.language}（{track.name}）：{exc}")
    return errors
