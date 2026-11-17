"""提取原生播放日志中的样本流请求证据；不保存 URL、签名、用户标识或媒体密钥。"""

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from lib.native import sample_vid


def media_request_fields(url: str, vid: str, decode_rounds: int) -> dict[str, Any] | None:
    """解析日志 URL；url 为原始或外层编码地址，vid 为精确样本 ID，decode_rounds 为解码上限；仅返回固定公开字段或 None。"""
    for _ in range(decode_rounds + 1):
        try:
            query = parse_qs(urlsplit(url).query)
        except ValueError:
            return None
        if query.get("vid") == [vid]:
            result = {}
            for key in ("type", "ccode", "app_ver", "duration"):
                values = query.get(key, [])
                # 限定公开元数据格式，不能把任意查询字符串当作报告内容。
                if len(values) == 1 and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", values[0]):
                    result[key] = values[0]
            return result if "type" in result else None
        decoded = unquote(url)
        if decoded == url:
            break
        url = decoded
    return None


def scan_playback_log(config: dict[str, Any]) -> dict[str, Any]:
    """只读扫描原生日志；config 指定 glob、字节预算及解码次数；返回精确匹配样本的流请求摘要，不能证明下载或解码完成。"""
    options = config["playback_log"]
    vid = sample_vid(config)
    result = {"observed_at_utc": datetime.now(timezone.utc).isoformat(), "sample": config["sample"],
              "read_bytes": 0, "files": [], "skipped": [], "budget_exhausted": False,
              "independent_file_verified": False,
              "note": "仅证明日志中出现该样本的媒体请求；不证明响应成功、当前权益或全片解码。"}
    for path in sorted(config["native_data"].glob(options["glob"])):
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > options["max_file_bytes"]:
            result["skipped"].append({"file": path.name, "reason": "FileTooLarge"})
            continue
        if result["read_bytes"] + size > options["max_total_bytes"]:
            result["budget_exhausted"] = True
            break
        groups = {}
        timestamp = None
        consumed = 0
        try:
            with path.open("rb") as source:
                # 固定采样开始时的大小；不追随运行中客户端不断增长的日志。
                line_number = 0
                while consumed < size:
                    raw = source.readline(size - consumed)
                    if not raw:
                        break
                    consumed += len(raw)
                    line_number += 1
                    line = raw.decode("utf-8", errors="replace")
                    match = re.search(options["timestamp_pattern"], line)
                    if match:
                        timestamp = match.group(1)
                    for url in re.findall(r'https?://[^\s"<>]+', line):
                        fields = media_request_fields(url, vid, options["decode_rounds"])
                        if fields is None:
                            continue
                        key = tuple(sorted(fields.items()))
                        if key not in groups:
                            groups[key] = dict(fields, occurrences=0, first_line=line_number,
                                               first_timestamp_local=timestamp)
                        row = groups[key]
                        row["occurrences"] += 1
                        row["last_line"] = line_number
                        row["last_timestamp_local"] = timestamp
        except OSError as error:
            result["skipped"].append({"file": path.name, "reason": type(error).__name__})
        result["read_bytes"] += consumed
        result["files"].append({"file": path.name, "sampled_bytes": consumed,
                                "size_at_start": size, "requests": list(groups.values())})
    return result
