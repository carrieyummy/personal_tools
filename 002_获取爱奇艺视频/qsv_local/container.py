"""解析 QSV v2 元数据、索引及外层混淆；不修改源文件。"""

import base64
import gzip
import json
from pathlib import Path
import struct
from typing import Callable


def unscramble(raw: bytes) -> bytes:
    """还原 QSV v2 索引或段头；raw 为完整混淆块；返回等长原始字节。"""
    data = bytearray(raw)
    state = 0x62677079
    for position in range(len(data) - 1, 0, -1):
        state = ((state << 1) | (state >> 31)) & 0xFFFFFFFF
        state ^= data[position]
    for position in range(1, len(data)):
        state ^= data[position]
        state = ((state >> 1) | (state << 31)) & 0xFFFFFFFF
        target = state % position
        previous = data[target]
        data[target] = previous ^ (~data[position] & 0xFF)
        data[position] = previous
    return bytes(data)


class QsvFile:
    """已验证结构的 QSV v2 文件及其元数据；本实现只支持独立音轨的 v5 下载样本。"""

    def __init__(self, path: Path):
        """读取索引和元数据；path 为已下载的 QSV 路径；结构不符时抛出异常。"""
        self.path = path
        total = path.stat().st_size
        with path.open("rb") as stream:
            header = stream.read(90)
            if len(header) != 90 or header[:10] != b"QIYI VIDEO":
                raise ValueError("不是完整的 QSV 文件")
            self.container_version = struct.unpack_from("<I", header, 10)[0]
            if self.container_version != 2:
                raise ValueError(f"当前处理器支持 QSV v2，检测到容器版本 {self.container_version}，需独立适配")
            metadata_offset, length, count = struct.unpack_from("<QII", header, 74)
            if not 0 < count < 100000 or not 8 < length <= 32 * 1024 * 1024:
                raise ValueError("QSV 索引或元数据长度异常")
            if metadata_offset + length > total:
                raise ValueError("QSV 元数据不完整")
            stream.seek(90 + ((count + 7) >> 3))
            self.segments = []
            for _ in range(count):
                raw = stream.read(28)
                if len(raw) != 28:
                    raise ValueError("QSV 索引截断")
                _, offset, size = struct.unpack("<16sQI", unscramble(raw))
                if size < 1024 or offset + size > total:
                    raise ValueError("QSV 视频段越界")
                self.segments.append((offset, size))
            stream.seek(metadata_offset)
            data = bytearray(stream.read(length))
        for index in range(len(data) // 4 * 4):
            data[index] ^= (0x79, 0x70, 0x67, 0x62)[index % 4]
        self.info = json.loads(data[8:-1])["qsv_info"]
        if str(self.info["license_version"]) != "5":
            raise ValueError(f"当前 SDK 适配器支持 v5，检测到授权版本 {self.info['license_version']}，需独立适配")
        sizes = list(map(int, self.info["vd"]["seg"]["size"]))
        if sizes != [size for _, size in self.segments]:
            raise ValueError("视频索引与元数据不一致")
        for current, following in zip(self.segments, self.segments[1:]):
            if current[0] + current[1] != following[0]:
                raise ValueError("不支持非连续的 QSV 视频段")
        if not self.info.get("independentaudio") or self.info["ad"]["ff"] != "amp4":
            raise ValueError("当前实现要求独立的 amp4 音轨")
        self.audio_sizes = list(map(int, self.info["ad"]["seg"]["size"]))
        self.audio_offset = max(offset + size for offset, size in self.segments)
        if not self.audio_sizes or min(self.audio_sizes) <= 0 or self.audio_offset + sum(self.audio_sizes) > total:
            raise ValueError("QSV 音轨不完整")

    def ticket(self) -> bytes:
        """读取文件自带的 v5 授权；返回 SDK 所需的 Base64 字节串，不持久化解包内容。"""
        return base64.b64decode(self.info["drmticket"]["ticketdata"].strip(), validate=True)

    def extract(self, video: Path, audio: Path, buffer_size: int,
                progress: Callable[[int, int], None] | None = None) -> None:
        """提取视频外层和音轨；video/audio 为新文件路径，buffer_size 为正复制块长；
        progress 可接收已读源字节及总源字节，无返回值。
        """
        if buffer_size <= 0:
            raise ValueError("复制缓冲区必须为正数")
        done, total = 0, sum(size for _, size in self.segments) + sum(self.audio_sizes)

        def advance(size: int) -> None:
            """累计本次读取的 size 字节并通知进度，无返回值。"""
            nonlocal done
            done += size
            if progress:
                progress(done, total)

        with self.path.open("rb") as source, video.open("xb") as target:
            for offset, size in self.segments:
                source.seek(offset)
                target.write(unscramble(source.read(1024)))
                advance(1024)
                self._copy(source, target, size - 1024, buffer_size, advance)
        with self.path.open("rb") as source, audio.open("xb") as target:
            source.seek(self.audio_offset)
            target.write(gzip.decompress(source.read(self.audio_sizes[0])))
            advance(self.audio_sizes[0])
            self._copy(source, target, sum(self.audio_sizes[1:]), buffer_size, advance)

    @staticmethod
    def _copy(source, target, remaining: int, buffer_size: int, progress: Callable[[int], None]) -> None:
        """复制精确字节数；source/target 为二进制流，remaining/buffer_size 为非负长度/正块长，
        progress 接收本次复制字节数，无返回值。
        """
        while remaining:
            chunk = source.read(min(remaining, buffer_size))
            if not chunk:
                raise ValueError("读取 QSV 时遇到截断")
            target.write(chunk)
            remaining -= len(chunk)
            progress(len(chunk))
