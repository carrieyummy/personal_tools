"""按客户端下载记录发现主 HLS 资源，并严格检查本地分片清单。"""

from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

from .native import HEADER_SIZE
from .configuration import series_directory_name


def discover(config: dict, video_id: str | None = None) -> dict:
    """按 config 和 video_id 查找唯一已完成主资源；返回含 series_id（vbarid）的任务，状态或编号无效时抛错。"""
    if video_id is None:
        tasks, _ = discover_all(config)
        if len(tasks) != 1:
            raise ValueError("验收要求选择唯一剧集")
        return tasks[0]
    root = ET.parse(config["download_record"]).getroot()
    matches = [item for item in root.iter("l") if item.get("id") == video_id]
    if len(matches) != 1:
        raise ValueError("下载记录中目标 video_id 不唯一或不存在")
    record = matches[0].attrib
    if record.get("cache_complete") != "true" or record.get("bHls") != "1":
        raise ValueError("客户端未确认此 HLS 资源下载完成")
    fid = record["fid"]
    if Path(fid).name != fid or "/" in fid or "\\" in fid or not fid.endswith(".hls"):
        raise ValueError("下载记录中的资源标识非法")
    folders = [p.resolve() for p in config["input_root"].glob("*/*.hls") if p.name == fid]
    if len(folders) != 1 or not folders[0].is_relative_to(config["input_root"]):
        raise ValueError("下载目录中目标资源不唯一、不存在或越界")
    title = re.sub(r'[\s<>:"/\\|?*\x00-\x1f]+', "_", record["fn"]).strip(". _")
    series = re.sub(r'[\s<>:"/\\|?*\x00-\x1f]+', "_", record["vbarname"]).strip(". _")
    if not title or not series or series in (".", ".."):
        raise ValueError("下载记录中的剧集标题为空")
    series_id = record.get("vbarid", "")
    series_directory_name(config, series, series_id)
    if int(record["download_size"]) != int(record["fs"]):
        raise ValueError("下载记录中的已下载大小与资源大小不一致")
    return {"video_id": record["id"], "title": title, "series": series, "series_id": series_id,
            "fid": fid, "folder": folders[0],
            "media_bytes": int(record["fs"]), "format_id": record["cfid"]}


def discover_all(config: dict) -> tuple[list[dict], list[dict]]:
    """依据 config 的筛选列表读取下载 XML；返回已完成任务和未就绪条目，不读取分片内容。"""
    root = ET.parse(config["download_record"]).getroot()
    selection = config["selection"]
    ids, names = set(selection["video_ids"]), set(selection["series_names"])
    selected = [item for item in root.iter("l") if not ids and not names
                or item.get("id") in ids or item.get("vbarname") in names]
    tasks, skipped, seen = [], [], set()
    for missing in sorted(ids - {item.get("id") for item in selected}):
        skipped.append({"video_id": missing, "status": "skipped", "detail": "指定视频 ID 未出现在下载记录"})
    for missing in sorted(names - {item.get("vbarname") for item in selected}):
        skipped.append({"video_id": missing, "status": "skipped", "detail": "指定剧名未出现在下载记录"})
    for item in selected:
        video_id = item.get("id", "")
        if video_id in seen:
            continue
        seen.add(video_id)
        try:
            tasks.append(discover(config, video_id))
        except (ValueError, KeyError, OSError) as error:
            skipped.append({"video_id": video_id, "status": "skipped", "detail": str(error)})
    tasks.sort(key=lambda task: (task["series"].casefold(), task["title"].casefold(), task["video_id"]))
    return tasks, skipped


def read_segments(folder: Path) -> tuple[list[dict], float]:
    """读取 folder 的本地清单，验证序号、大小和完成标记；返回有序分片列表与总时长秒数。

    当前仅适配主流为 MPEG-TS 的 HVN 下载；fMP4、显式 HLS 加密和不连续时间线需独立适配。
    """
    lines = (folder / ".m3u8").read_text(encoding="utf-8-sig").splitlines()
    if not lines or lines[0] != "#EXTM3U" or "#EXT-X-ENDLIST" not in lines:
        raise ValueError("HLS 清单不完整")
    if any(line.startswith(("#EXT-X-MAP", "#EXT-X-KEY", "#EXT-X-DISCONTINUITY")) for line in lines):
        raise ValueError("当前处理器只支持无 MAP/KEY/DISCONTINUITY 标签的主 TS 清单")
    files = {}
    for path in folder.rglob("*.ts"):
        if not path.stem.isdigit() or int(path.stem) in files:
            raise ValueError("分片文件名非数字或序号重复")
        resolved = path.resolve()
        if not resolved.is_relative_to(folder):
            raise ValueError("分片路径超出资源目录")
        files[int(path.stem)] = resolved
    if any(folder.rglob("*.cfg")):
        raise ValueError("资源目录仍存在分片下载临时标记")
    segments, duration, pending_duration = [], 0.0, None
    for line in lines:
        if line.startswith("#EXTINF:"):
            pending_duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line and not line.startswith("#"):
            if pending_duration is None or not 0 < pending_duration < float("inf"):
                raise ValueError("分片时长缺失或非法")
            query = parse_qs(urlsplit(line).query, strict_parsing=True)
            index = int(query["index"][0])
            length = int(query["bre"][0]) - int(query["brs"][0]) + 1
            if index != len(segments) or length <= 0 or index not in files:
                raise ValueError("分片序号不连续、长度非法或缺少分片")
            path = files[index]
            stat = path.stat()
            if stat.st_size != length + HEADER_SIZE:
                raise ValueError(f"分片长度与清单不一致：{path.name}")
            segments.append({"index": index, "path": path, "media_bytes": length,
                             "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
            duration += pending_duration
            pending_duration = None
    if not segments or len(files) != len(segments) or pending_duration is not None:
        raise ValueError("分片集合与清单不一致")
    return segments, duration
