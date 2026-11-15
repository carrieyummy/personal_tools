"""通过本机客户端还原单集主 TS 分片并计算源文件摘要。"""

import hashlib
from pathlib import Path

from .native import CacheLibrary, CacheReader, HEADER_SIZE
from .runtime import check_cancel


def extract(config: dict, segments: list[dict], folder: Path, target: Path, *, cancel_event=None, notify=None) -> list[dict]:
    """按 config 将 folder 的 segments 还原至新 target；cancel_event 可取消、notify 接收进度；返回来源摘要。"""
    library = CacheLibrary(config["native"])
    fingerprints = []
    with target.open("xb") as output:
        for number, segment in enumerate(segments, 1):
            check_cancel(cancel_event)
            source, length = segment["path"], segment["media_bytes"]
            if length % 188:
                raise ValueError("源媒体长度不是完整 TS 包的整数倍")
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                header = stream.read(HEADER_SIZE)
                digest.update(header)
                with CacheReader(library, header, folder) as reader:
                    offset = 0
                    while offset < length:
                        check_cancel(cancel_event)
                        data = stream.read(min(config["copy_buffer_bytes"], length - offset))
                        if not data:
                            raise ValueError(f"源分片读取中途截断：{source.name}")
                        digest.update(data)
                        clear = reader.decrypt(data, offset)
                        if any(value != 0x47 for value in clear[(-offset) % 188::188]):
                            raise ValueError(f"还原后 TS 包失同步：{source.name}")
                        output.write(clear)
                        offset += len(data)
                    if stream.read(1):
                        raise ValueError("读取期间分片长度发生变化")
            fingerprints.append({"path": str(source), "size": segment["size"], "sha256": digest.hexdigest()})
            if number == 1 or number % 20 == 0 or number == len(segments):
                if notify:
                    notify(number * 100 / len(segments))
                else:
                    print(f"还原分片 {number}/{len(segments)}", flush=True)
    return fingerprints


