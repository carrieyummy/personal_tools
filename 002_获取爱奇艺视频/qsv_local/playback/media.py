"""有界验证在线播放清单及授权媒体分段，复用项目已有的 TS 和本机 SDK 适配。"""

from pathlib import Path
from copy import deepcopy
from contextlib import contextmanager
from http.client import HTTPException
import json
import ast
import hashlib
import math
import re
import subprocess
from threading import Event
import uuid
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request

import yaml

from ..configuration import resolve_path, validate_concurrency
from ..storage import validate_lock_file
from ..native import NativeV5, latest_dll
from ..process import probe_media
from ..transport import TransportDecoder
from .network import open_network
from .naming import validate_date_prefix


def parse_manifest(body: bytes, base_url: str, host_suffix: str) -> dict:
    """解析 body 的完整 HLS 媒体清单；base_url 解析相对地址，host_suffix 限定域名；返回内存分段和结构摘要。"""
    text = body.decode("utf-8-sig").strip()
    if not text.startswith("#EXTM3U") or "#EXT-X-ENDLIST" not in text:
        raise ValueError("需要已结束的 HLS 点播媒体清单")
    segments, methods, duration = [], set(), None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("#EXT-X-STREAM-INF", "#EXT-X-MAP", "#EXT-X-BYTERANGE")):
            raise ValueError("当前 TS 研究适配不支持主清单、初始化映射或 HLS BYTERANGE")
        if line.startswith("#EXT-X-KEY:"):
            match = re.search(r"(?:^|,)METHOD=([^,]+)", line.split(":", 1)[1])
            if not match:
                raise ValueError("HLS 密钥标记未说明方法")
            methods.add(match[1])
        elif line.startswith("#EXTINF:"):
            if duration is not None:
                raise ValueError("HLS 分段时长未对应唯一媒体地址")
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("HLS 分段时长必须为正数")
        elif line and not line.startswith("#"):
            parsed = urlsplit(urljoin(base_url, line))
            if (duration is None or parsed.scheme not in ("http", "https") or not parsed.hostname
                    or not parsed.hostname.endswith(host_suffix)):
                raise ValueError("HLS 媒体地址或时长无效")
            segments.append({"url": parsed.geturl(), "duration": duration})
            duration = None
    if not segments or duration is not None:
        raise ValueError("HLS 清单为空或存在缺失地址")
    return {"segments": segments, "methods": sorted(methods), "duration": sum(row["duration"] for row in segments)}


def media_config(profile: dict) -> dict:
    """从 profile 引用的主配置读取媒体验证及 SDK 参数；返回独立字典，不读取离线下载记录。"""
    path = profile["main_config"]
    config = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    validate_lock_file(config["logging"]["lock_file"])
    if "_naming_snapshot" in profile:
        config["naming"] = deepcopy(profile["_naming_snapshot"])
    validate_date_prefix(config["naming"])
    validate_concurrency(config)
    if not isinstance(config.get("audio"), dict) or type(config["audio"].get("quality")) is not int or config["audio"]["quality"] <= 0:
        raise ValueError("audio.quality 必须为正整数；1 表示最高声明音轨")
    output_root = resolve_path(config["output_path"], path.parent)
    logging = config["logging"]
    for key, extension in (("playback_state_file", ".json"), ("playback_html_file", ".html")):
        value = logging[key]
        if not isinstance(value, str) or Path(value).name != value or Path(value).suffix.lower() != extension:
            raise ValueError(f"logging.{key} 必须是输出根目录内的 {extension} 文件名")
        if not (output_root / value).resolve().is_relative_to(output_root):
            raise ValueError("链接日志路径超出输出目录")
    if type(logging["open_browser"]) is not bool:
        raise ValueError("logging.open_browser 必须为布尔值")
    if type(logging["final_state_timeout_seconds"]) not in (int, float) or not math.isfinite(logging["final_state_timeout_seconds"]) or logging["final_state_timeout_seconds"] <= 0:
        raise ValueError("logging.final_state_timeout_seconds 必须为大于零的有限秒数")
    config["ffmpeg_path"] = resolve_path(config["ffmpeg_path"], path.parent)
    native_path = resolve_path(config["native_profile"], path.parent)
    native = yaml.safe_load(native_path.read_text(encoding="utf-8-sig"))
    for name in ("dll_root", "cert_path"):
        native[name] = resolve_path(native[name], native_path.parent)
    library = latest_dll(native["dll_root"], native["dll_name"])
    if (native["dll_name"].casefold() != profile["playback"]["player"]["module"].casefold()
            or hashlib.sha256(library.read_bytes()).hexdigest() != profile["playback"]["player"]["sha256"]):
        raise RuntimeError("用于媒体授权的最新 SDK 与已核验的播放版本不一致")
    config["sdk"] = native
    return config


def signed_segment(segment: dict, source: dict) -> str:
    """对应 segment/source 的调度授权：旧格式按文件读取 mk，新格式按 uea/ms 加清单内 qd_tc；返回内存地址。"""
    parsed = urlsplit(segment["url"])
    signatures = source.get("mk")
    signature = signatures.get(Path(parsed.path).name) if isinstance(signatures, dict) else None
    query = parse_qs(parsed.query)
    if (signatures is None and source.get("uea") == 1
            and all(len(query.get(name, [])) == 1 and query[name][0] for name in ("qd_tc", "qd_rt", "qd_etd", "qd_tvid"))):
        signature = source.get("ms")
    if not isinstance(signature, str) or not signature:
        raise ValueError("播放源缺少当前媒体文件的调度签名")
    if set(parse_qs(parsed.query)) & set(parse_qs(signature)):
        raise ValueError("媒体地址与原生调度签名存在重复参数，不猜测覆盖顺序")
    return parsed._replace(query=parsed.query + ("&" if parsed.query else "") + signature).geturl()


class MediaNetworkError(RuntimeError):
    """表示可重试的媒体网络错误；异常信息仅保留状态码或错误类型，不包含临时授权地址。"""


def dispatch_address(body: bytes) -> str:
    """从 body 的 JSON 或原生 var 字符串调度中提取唯一 URL；只解析字面量，不执行 JavaScript，返回内存地址。"""
    text = body.decode("utf-8-sig").strip()
    wrapped = re.fullmatch(r"var\s+videoUrl\s*=\s*(\{.*\})\s*;?", text, re.S)
    if wrapped:
        # 原生端点返回 JavaScript 对象字面量；只将字符串外的属性名规范化为 JSON，绝不执行返回脚本。
        chunks = re.split(r'''("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')''', wrapped[1])
        normalized = []
        for index, chunk in enumerate(chunks):
            if index % 2:
                normalized.append(json.dumps(ast.literal_eval(chunk.replace(r"\/", "/"))))
            else:
                normalized.append(re.sub(r'([,{]\s*)([a-zA-Z_$][\w$]*)(\s*:)', r'\1"\2"\3', chunk))
        payload = json.loads("".join(normalized))
        if payload.get("code") != "A00000":
            code = payload.get("code")
            raise MediaNetworkError(f"原生媒体调度失败：{code if isinstance(code, str) and re.fullmatch('[A-Za-z0-9_-]+', code) else '未知状态'}")
        value = payload.get("data")
        if isinstance(value, dict) and isinstance(value.get("l"), str):
            return value["l"]
        raise RuntimeError(f"原生媒体调度缺少地址，结构字段：{sorted(value) if isinstance(value, dict) else type(value).__name__}")
    if text.startswith("{"):
        value = json.loads(text)
        if isinstance(value.get("l"), str):
            return value["l"]
    addresses = set()
    for match in re.finditer(r'''\bvar\s+[a-zA-Z_$][\w$]*\s*=\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\s*(?:;|$)''', text):
        literal = match[1]
        try:
            value = ast.literal_eval(literal.replace(r"\/", "/"))
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, str) and urlsplit(value).scheme in ("http", "https"):
            addresses.add(value)
    if len(addresses) != 1:
        raise RuntimeError("媒体调度未返回唯一的已适配地址，未执行响应脚本")
    return addresses.pop()


def read_response(response, size: int) -> bytes:
    """从网络 response 最多读取 size 字节；返回内容，将网络读取错误转为不含地址的可重试异常。"""
    try:
        return response.read(size)
    except (HTTPException, OSError) as error:
        raise MediaNetworkError(f"读取媒体分段失败：{type(error).__name__}") from None


@contextmanager
def segment_response(profile: dict, url: str, headers: dict, maximum: int, range_style: str = "header"):
    """打开 profile/url 的媒体，公开 headers、maximum 限制边界，range_style 为 header 或完整物理块 whole；提供响应并自动关闭。"""
    if range_style not in ("header", "whole"):
        raise ValueError("未知的媒体范围协议")
    stages = []
    query = parse_qs(urlsplit(url).query)
    span = None
    if "start" in query or "end" in query:
        if any(len(query.get(key, [])) != 1 for key in ("start", "end")):
            raise ValueError("媒体地址未唯一指定完整区间")
        start, end = int(query["start"][0]), int(query["end"][0])
        if start < 0 or end <= start or end - start > maximum:
            raise ValueError("媒体声明区间无效或超过配置上限")
        span = (start, end)
    request_headers = dict(headers)
    if span is not None:
        request_headers = {key: value for key, value in headers.items() if key.casefold() != "range"}
        if range_style == "header":
            request_headers["Range"] = f"bytes={span[0]}-{span[1] - 1}"
        elif span[0] != 0:
            raise ValueError("完整物理块不能从非零字节开始")
    for _ in range(2):
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or not any(parsed.hostname.endswith(suffix) for suffix in profile["playback"]["cdn_host_suffixes"])):
            raise ValueError("调度跳转不属于已核验的媒体域名")
        if span is not None:
            # 与原生 HCDN 一致：URL 负责资源授权，HTTP Range 负责字节区间，不依赖 CDN 对查询参数的解释。
            clean_query = "&".join(value for value in parsed.query.split("&") if unquote(value.partition("=")[0]) not in ("start", "end", "contentlength"))
            url = parsed._replace(query=clean_query).geturl()
        try:
            response = open_network(profile, Request(url, headers=request_headers))
        except HTTPError as error:
            body = error.read(min(profile["network"]["max_response_bytes"], 4096))
            server = error.headers.get("Server", "未知服务")
            error.close()
            markers = [word for word in ("auth", "expired", "sign", "key", "referer", "forbidden") if word.encode() in body.lower()]
            raise MediaNetworkError(f"获取媒体分段返回 HTTP {error.code}；服务={server}，响应字节={len(body)}，诊断标记={markers}") from None
        except (URLError, OSError) as error:
            raise MediaNetworkError(f"获取媒体分段网络失败：{type(error).__name__}") from None
        try:
            host = urlsplit(response.url).hostname
            if not host or not any(host.endswith(suffix) for suffix in profile["playback"]["cdn_host_suffixes"]):
                raise ValueError("媒体响应不属于已核验的 CDN 域名")
            shape = {"host": host, "status": response.status, "content_type": response.headers.get("Content-Type"),
                     "content_range": response.headers.get("Content-Range")}
            prefix = read_response(response, min(32, maximum + 1))
            stages.append(shape)
            if prefix.lstrip(b"\xef\xbb\xbf \r\n\t").startswith((b"{", b"var ")):
                limit = profile["network"]["max_response_bytes"]
                raw = prefix + read_response(response, max(0, limit + 1 - len(prefix)))
                if len(raw) > limit:
                    raise ValueError("媒体调度响应超过配置上限")
                url = dispatch_address(raw)
                continue
            if span is not None and shape["content_range"] is not None:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", shape["content_range"])
                if (match is None or (int(match[1]), int(match[2]) + 1) != span
                        or match[3] != "*" and int(match[3]) < span[1]):
                    raise RuntimeError("媒体服务器返回的 Content-Range 与目标区间不一致")
            yield response, prefix, span, {"stages": stages}
            return
        finally:
            response.close()
    raise RuntimeError("媒体调度超过已验证的一次 JSON 跳转")


def segment_chunks(response, prefix: bytes, span: tuple | None, maximum: int, buffer_bytes: int,
                   cancel_event: Event | None = None):
    """从 response/prefix 按 buffer_bytes 读取，受 span/maximum 和可选 cancel_event 约束；产出媒体块，长度不符时抛错。"""
    limit, total, chunk = span[1] - span[0] if span else maximum, 0, prefix
    while chunk:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("媒体获取已取消")
        total += len(chunk)
        if total > limit:
            raise RuntimeError("媒体服务器未遵守请求的字节区间")
        yield chunk
        chunk = read_response(response, min(buffer_bytes, limit + 1 - total))
    if span is not None and total != limit:
        raise MediaNetworkError(f"媒体服务器返回了不完整的字节区间：实际 {total}，预期 {limit}")


def fetch_segment(profile: dict, url: str, headers: dict, maximum: int, *, range_style: str = "header") -> tuple[bytes, dict]:
    """按 profile/url/公开 headers 获取 maximum 内的样本；range_style 选择范围或整块协议，返回内容和安全诊断。"""
    with segment_response(profile, url, headers, maximum, range_style) as (response, prefix, span, shape):
        return b"".join(segment_chunks(response, prefix, span, maximum, maximum)), shape


def save_segment(profile: dict, url: str, headers: dict, target: Path, expected_bytes: int, buffer_bytes: int,
                 cancel_event: Event | None = None, *, progress=None, range_style: str = "header") -> dict:
    """流式保存 profile/url/公开 headers 到 target；expected_bytes/ buffer_bytes 指定长度和块长；响应 cancel_event，progress 接收字节，range_style 选择协议，返回指纹。"""
    if expected_bytes <= 0 or buffer_bytes <= 0:
        raise ValueError("完整分段的预期长度和缓冲大小必须为正整数")
    if target.exists():
        raise FileExistsError(f"完整分段目标已经存在：{target}")
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".partial")
    digest, total = hashlib.sha256(), 0
    with segment_response(profile, url, headers, expected_bytes, range_style) as (response, prefix, span, _):
        if span is None or span[1] - span[0] != expected_bytes:
            raise ValueError("完整分段范围与清单预期长度不一致")
        with temporary.open("xb") as stream:
            for chunk in segment_chunks(response, prefix, span, expected_bytes, buffer_bytes, cancel_event):
                stream.write(chunk)
                digest.update(chunk)
                total += len(chunk)
                if progress:
                    progress(total)
    temporary.rename(target)
    stamp = target.stat()
    return {"size_bytes": total, "mtime_ns": stamp.st_mtime_ns, "sha256": digest.hexdigest()}


def sample_media(profile: dict, source: dict, body: bytes, work: Path, headers: dict) -> dict:
    """按 profile 验证 source/body 清单；work 为新媒体目录，headers 仅含公开 UA；返回无票据样本结果。"""
    settings = profile["playback"]
    plan = parse_manifest(body, source["ml"], settings["resource_host_suffix"])
    ticket = source.get("drm", {}).get("ticket")
    if not isinstance(ticket, str) or not ticket:
        raise ValueError("当前播放源没有可交给本机 SDK 的授权")
    work.mkdir(parents=True, exist_ok=False)
    config, results = media_config(profile), []
    count = len(plan["segments"])
    indices = sorted({round(value * (count - 1)) for value in settings["media_probe"]["positions"]})
    with NativeV5(config["sdk"], ticket.encode("ascii")) as sdk:
        for index in indices:
            record = {"index": index, "expected_seconds": plan["segments"][index]["duration"]}
            parsed = urlsplit(plan["segments"][index]["url"])
            query = parse_qs(parsed.query)
            record["request_shape"] = {"scheme": parsed.scheme, "host": parsed.hostname, "suffix": Path(parsed.path).suffix,
                                       "query_fields": sorted(query),
                                       "range": {key: query[key] for key in ("start", "end", "contentlength") if key in query}}
            results.append(record)
            outer, clear = work / f"{index:05d}_outer.ts", work / f"{index:05d}_clear.ts"
            try:
                url = signed_segment(plan["segments"][index], source)
                raw, record["response_shape"] = fetch_segment(profile, url, headers, settings["media_probe"]["max_segment_bytes"])
                outer.write_bytes(raw)
                record["bytes"] = len(raw)
                record["conversion"] = TransportDecoder(sdk, config["max_pes_bytes"]).convert(outer, clear)
                record["media"] = probe_media(config, clear, work / f"{index:05d}_media.log")
                process = subprocess.run([str(config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-v", "error", "-xerror",
                                          "-err_detect", "explode", "-i", str(clear), "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
                                         stdin=subprocess.DEVNULL, capture_output=True, timeout=config["process_timeout_seconds"],
                                         creationflags=subprocess.CREATE_NO_WINDOW, check=False)
                (work / f"{index:05d}_decode.log").write_bytes(process.stderr)
                record["decode_exit_code"] = process.returncode
                record["verified"] = process.returncode == 0
            except Exception as error:
                record.update(verified=False, error=str(error))
    return {"segment_count": count, "duration": plan["duration"], "methods": plan["methods"], "samples": results,
            "all_samples_verified": bool(results) and all(row["verified"] for row in results)}
