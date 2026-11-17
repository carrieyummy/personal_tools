"""解析优酷 TS 媒体清单并隔离 CDN 访问；不记录签名地址。"""
import json
import math
import re
from collections import Counter
from typing import Any
from ..errors import ProtocolError
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

def require_media_url(url: str, domains: list[str], schemes: list[str]) -> None:
    """检查媒体目的地；url 为内存地址，domains/schemes 为允许域名与协议；拒绝凭据和未授权目标。"""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme not in schemes or parsed.username or parsed.password or parsed.port not in (None, 80, 443)
            or not any(host == domain or host.endswith("." + domain) for domain in domains)):
        raise ProtocolError("MediaDestinationRejected")

class MediaRedirect(HTTPRedirectHandler):
    """仅允许配置中的媒体 CDN 重定向；此连接不装载账号 Cookie 或完整设备 UA。"""

    def __init__(self, domains: list[str], schemes: list[str]) -> None:
        """保存允许域名后缀；domains/schemes 为非空字符串列表；无返回值。"""
        self.domains, self.schemes = domains, schemes
        super().__init__()

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        """逐跳校验媒体重定向；参数来自 urllib 协议；返回新请求，非法目标抛固定错误。"""
        require_media_url(newurl, self.domains, self.schemes)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def fetch_media(options: dict[str, Any], url: str, limit: int) -> tuple[bytes, str, dict[str, Any]]:
    """有界读取一个媒体资源；options 为媒体配置，url 仅驻内存，limit 为字节上限；返回字节、最终地址和脱敏状态。"""
    require_media_url(url, options["allowed_domains"], options["allowed_schemes"])
    opener = build_opener(ProxyHandler({"http":options["proxy"],"https":options["proxy"]} if options.get("proxy") else {}), MediaRedirect(options["allowed_domains"], options["allowed_schemes"]))
    request = Request(url, headers={"User-Agent": options["user_agent"], "Accept-Encoding": "identity"})
    try:
        with opener.open(request, timeout=options["timeout_seconds"]) as response:
            if response.status != 200:
                raise ProtocolError("MediaUnexpectedStatus")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > limit):
                raise ProtocolError("MediaInvalidContentLength")
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ProtocolError("MediaResponseTooLarge")
            if length is not None and len(data) != int(length):
                raise ProtocolError("MediaTruncatedResponse")
            return data, response.url, {"http_status": response.status, "bytes": len(data),
                                       "content_length": int(length) if length else None,
                                       "host": urlsplit(response.url).hostname}
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise ProtocolError("MediaFetchFailed: " + type(error).__name__) from None

def parse_manifest(raw: bytes, base: str) -> dict[str, Any]:
    """解析当前已验证的完整 TS 媒体清单；raw 为 UTF-8 清单，base 为最终地址；返回含内存 URL 的分段结构。"""
    lines = raw.decode("utf-8-sig").splitlines()
    if not lines or lines[0].strip() != "#EXTM3U" or "#EXT-X-ENDLIST" not in lines:
        raise ProtocolError("MediaManifestNotComplete")
    segments, tags = [], Counter()
    duration, private, discontinuity = None, {}, 0
    key_methods = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            tag, _, value = line.partition(":")
            tags[tag] += 1
            if tag in ("#EXT-X-STREAM-INF", "#EXT-X-MAP", "#EXT-X-BYTERANGE", "#EXT-X-GAP"):
                raise ProtocolError("MediaManifestStructureUnsupported: " + tag)
            if tag == "#EXTINF":
                if duration is not None:
                    raise ProtocolError("MediaManifestMissingUri")
                duration = float(value.split(",", 1)[0])
                if not math.isfinite(duration) or duration <= 0:
                    raise ProtocolError("MediaManifestInvalidDuration")
            elif tag == "#EXT-X-PRIVINF":
                private = {}
                for attribute in value.split(","):
                    name, separator, number = attribute.strip().partition("=")
                    if name in private or name not in ("FILESIZE", "DRM_NOTENCRYPT"):
                        raise ProtocolError("MediaManifestUnsupportedPrivateAttribute")
                    # 优酷实测明文标记是无等号的 DRM_NOTENCRYPT，不能按缺失值当作密文。
                    if name == "DRM_NOTENCRYPT" and not separator:
                        private[name] = 1
                    elif not separator or not number.isdecimal():
                        raise ProtocolError("MediaManifestInvalidPrivateValue")
                    else:
                        private[name] = int(number)
                if private.get("DRM_NOTENCRYPT", 0) not in (0, 1) or private.get("FILESIZE", 1) <= 0:
                    raise ProtocolError("MediaManifestInvalidPrivateValue")
            elif tag == "#EXT-X-DISCONTINUITY":
                discontinuity += 1
            elif tag == "#EXT-X-KEY":
                method = re.search(r"(?:^|,)METHOD=([A-Z0-9-]+)(?:,|$)", value)
                if not method:
                    raise ProtocolError("MediaManifestInvalidKeyTag")
                key_methods.add(method[1])
        else:
            if duration is None:
                raise ProtocolError("MediaManifestMissingDuration")
            segments.append({"url": urljoin(base, line), "duration": duration,
                             "private": private, "discontinuity": discontinuity})
            duration, private = None, {}
    if not segments or duration is not None:
        raise ProtocolError("MediaManifestIncompleteSegments")
    return {"segments": segments, "tags": dict(tags), "key_methods": sorted(key_methods),
            "duration": sum(segment["duration"] for segment in segments)}

def transport_summary(data: bytes) -> dict[str, Any]:
    """检查 TS 同步、包长和传输加扰位；data 为完整抽样段；返回结构证据，不据此断言 PES 明文。"""
    aligned = bool(data) and len(data) % 188 == 0 and all(data[i] == 0x47 for i in range(0, len(data), 188))
    result = {"ts_aligned": aligned, "bytes_mod_188": len(data) % 188}
    if aligned:
        result["scrambling_control_counts"] = dict(Counter((data[i + 3] >> 6) & 3 for i in range(0, len(data), 188)))
    return result
