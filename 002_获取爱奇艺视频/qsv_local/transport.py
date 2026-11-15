"""按 TS 分段标记处理加密或明文视频 PES，保留 PTS/DTS、PCR 和音轨。"""

from collections import Counter
from pathlib import Path
import re
from typing import Callable

from .native import NativeV5

START_CODE = re.compile(rb"\x00\x00\x00?\x01")
PACKET_SIZE = 188


def payload_offset(packet: bytes) -> int:
    """读取 TS 负载偏移；packet 必须为一个完整 TS 包；返回 4～188 的偏移。"""
    if len(packet) != PACKET_SIZE or packet[0] != 0x47 or packet[1] & 0x80:
        raise ValueError("TS 包截断、失同步或传输错误")
    if not packet[3] & 0x10:
        return PACKET_SIZE
    offset = 5 + packet[4] if packet[3] & 0x20 else 4
    if offset > PACKET_SIZE:
        raise ValueError("TS 适配字段越界")
    return offset


def section_payload(packet: bytes) -> bytes:
    """解析单包 PSI；packet 为含 PUSI 的 TS 包；返回完整 section，不支持跨包 PSI。"""
    offset = payload_offset(packet)
    if not packet[1] & 0x40 or offset >= PACKET_SIZE:
        raise ValueError("不支持分散的 PSI section")
    start = offset + 1 + packet[offset]
    section = packet[start:]
    if len(section) < 3:
        raise ValueError("PSI section 头截断")
    size = 3 + ((section[1] & 15) << 8) + section[2]
    if len(section) < size:
        raise ValueError("当前实现不支持跨包 PSI section")
    return section[:size]


def service_descriptor(packet: bytes) -> bytes | None:
    """解析 SDT 的服务名；packet 为 SDT 包；识别起始或管道分隔的 mdcm 标记，返回原始描述符，明文返回 None。

    描述符字段随视频而异，完整原文交给当前已验证的 SDK 解释，不要求固定存在 gv 字段。
    """
    section = section_payload(packet)
    position = 11
    while position + 5 <= len(section) - 4:
        length = ((section[position + 3] & 15) << 8) + section[position + 4]
        end = position + 5 + length
        position += 5
        while position + 2 <= end:
            tag, size = section[position:position + 2]
            data = section[position + 2:position + 2 + size]
            if len(data) != size:
                raise ValueError("SDT 描述符截断")
            if tag == 0x48 and len(data) >= 3:
                name_position = 2 + data[1]
                if name_position >= len(data):
                    raise ValueError("SDT 服务名截断")
                name = data[name_position + 1:name_position + 1 + data[name_position]]
                marker = name.lower()
                if marker.startswith(b"mdcm|") or b"|mdcm|" in marker:
                    return name
                if b"mdcm" in marker:
                    raise ValueError("不支持的 mdcm 分段描述符格式")
            position += 2 + size
        if position != end:
            raise ValueError("SDT 描述符边界不一致")
    return None


class TransportDecoder:
    """按原始时间戳和分段描述处理 TS 视频；不输出、提取或保存内容密钥。"""

    def __init__(self, sdk: NativeV5, max_pes_bytes: int):
        """创建转换器；sdk 为已接受授权的上下文，max_pes_bytes 是单个 PES 缓冲上限。"""
        self.sdk = sdk
        self.max_pes_bytes = max_pes_bytes
        self.pmt_pid = None
        self.video_pid = None
        self.stream_type = None
        self.encrypted = False
        self.pending = []
        self.pending_size = 0
        self.continuity = 15
        self.stats = Counter()
        self.descriptor_styles = Counter()

    def _tables(self, packet: bytes, pid: int) -> None:
        """处理 PAT/PMT；packet 为完整 TS 包，pid 为其 PID；记录唯一视频轨信息。"""
        if pid == 0:
            section = section_payload(packet)
            programs = [((section[i + 2] & 31) << 8) + section[i + 3]
                        for i in range(8, len(section) - 4, 4) if section[i:i + 2] != b"\0\0"]
            if len(programs) != 1:
                raise ValueError("要求单节目 TS")
            self.pmt_pid = programs[0]
        elif pid == self.pmt_pid:
            section = section_payload(packet)
            position = 12 + ((section[10] & 15) << 8) + section[11]
            videos = []
            while position + 5 <= len(section) - 4:
                kind = section[position]
                stream_pid = ((section[position + 1] & 31) << 8) + section[position + 2]
                if kind in (0x1B, 0x24):
                    videos.append((stream_pid, kind))
                position += 5 + ((section[position + 3] & 15) << 8) + section[position + 4]
            if len(videos) != 1:
                raise ValueError("要求唯一的 AVC/HEVC 视频轨")
            if self.video_pid is not None and videos[0] != (self.video_pid, self.stream_type):
                raise ValueError("视频轨在文件中发生变化")
            self.video_pid, self.stream_type = videos[0]

    def _decrypt_pes(self, pes: bytes) -> bytes:
        """解密一个完整视频 PES；pes 含头和 NAL；返回时间戳不变的 PES。"""
        if len(pes) < 9 or pes[:3] != b"\0\0\1":
            raise ValueError("视频 PES 头无效")
        header_end = 9 + pes[8]
        matches = list(START_CODE.finditer(pes, header_end))
        if not matches or matches[0].start() != header_end:
            raise ValueError("视频 PES 未以 Annex B NAL 开始")
        output = bytearray(pes[:header_end])
        nal_header = 2 if self.stream_type == 0x24 else 1
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(pes)
            nal = pes[match.end():end]
            if len(nal) < nal_header + 4:
                raise ValueError("加密 NAL 过短")
            raw = nal[nal_header:].replace(b"\0\0\3", b"\0\0")
            clear = self.sdk.decrypt(raw)
            output.extend(match.group())
            output.extend(nal[:nal_header])
            # SDK 已返回原始 EBSP，其中保留编码器的防竞争字节，不能再次插入 0x03。
            output.extend(clear)
            self.stats["decrypted_nals"] += 1
        if pes[4:6] != b"\0\0":
            length = len(output) - 6
            if length > 65535:
                raise ValueError("视频 PES 长度字段溢出")
            output[4:6] = length.to_bytes(2, "big")
        return bytes(output)

    def _packet(self, original: bytes, payload: bytes, first: bool) -> bytes:
        """重建视频 TS 包；original 提供 PCR，payload 最多容纳原负载，first 标记 PES 首包。"""
        header = bytearray(original[:4])
        header[1] = (header[1] & ~0x40) | (0x40 if first and payload else 0)
        if payload:
            self.continuity = (self.continuity + 1) & 15
        extra = 184 - len(payload)
        if extra == 0:
            header[3] = 0x10 | self.continuity
            return bytes(header) + payload
        adaptation = original[5:5 + original[4]] if original[3] & 0x20 else b""
        if len(adaptation) > extra - 1:
            raise ValueError("PCR 适配字段空间不足")
        if extra > 1 and not adaptation:
            adaptation = b"\0"
        adaptation += b"\xff" * (extra - 1 - len(adaptation))
        header[3] = (0x30 if payload else 0x20) | self.continuity
        return bytes(header) + bytes([extra - 1]) + adaptation + payload

    def _flush(self, output) -> None:
        """处理积累的完整 PES；output 为可写二进制流；保留穿插的非视频包顺序。"""
        if not self.pending:
            return
        video = [(i, packet, payload_offset(packet)) for i, packet in enumerate(self.pending)
                 if ((packet[1] & 31) << 8) + packet[2] == self.video_pid]
        pes = b"".join(packet[offset:] for _, packet, offset in video)
        if self.encrypted:
            pes = self._decrypt_pes(pes)
            self.stats["encrypted_pes"] += 1
        else:
            self.stats["plain_pes"] += 1
        self.stats["video_pes"] += 1
        cursor = 0
        last_video = video[-1][0]
        first = True
        for index, packet in enumerate(self.pending):
            pid = ((packet[1] & 31) << 8) + packet[2]
            if pid != self.video_pid:
                output.write(packet)
                continue
            capacity = PACKET_SIZE - payload_offset(packet)
            part = pes[cursor:cursor + capacity]
            output.write(self._packet(packet, part, first))
            cursor += len(part)
            first = False
            if index == last_video:
                while cursor < len(pes):
                    part = pes[cursor:cursor + 184]
                    template = bytes([0x47, self.video_pid >> 8, self.video_pid & 255, 0x10])
                    output.write(self._packet(template, part, False))
                    cursor += len(part)
        self.pending.clear()
        self.pending_size = 0

    def convert(self, source: Path, target: Path, progress: Callable[[int, int], None] | None = None,
                progress_bytes: int | None = None) -> dict:
        """转换完整 TS；source 为外层解包文件，target 必须不存在；
        progress 接收已读/总字节，启用时 progress_bytes 必须为正通知间隔；返回处理统计。
        """
        if progress and (type(progress_bytes) is not int or progress_bytes <= 0):
            raise ValueError("启用传输进度时必须指定正整数 progress_bytes")
        done, next_progress, total = 0, progress_bytes, source.stat().st_size
        with source.open("rb") as stream, target.open("xb") as output:
            while packet := stream.read(PACKET_SIZE):
                done += len(packet)
                if progress and done >= next_progress:
                    progress(done, total)
                    next_progress = done + progress_bytes
                payload_offset(packet)
                if packet[3] & 0xC0:
                    raise ValueError("不支持 TS 包级加密标记")
                pid = ((packet[1] & 31) << 8) + packet[2]
                if pid == 17:
                    self._flush(output)
                    descriptor = service_descriptor(packet)
                    self.encrypted = descriptor is not None
                    if descriptor:
                        style = "mdcm_prefix" if descriptor.lower().startswith(b"mdcm|") else "mdcm_embedded"
                        self.descriptor_styles[style] += 1
                        self.sdk.configure(descriptor)
                        self.stats["encrypted_segments"] += 1
                    output.write(packet)
                    continue
                self._tables(packet, pid)
                if pid == self.video_pid and packet[1] & 0x40:
                    self._flush(output)
                if pid == self.video_pid or self.pending:
                    if pid == self.video_pid and not self.pending and not packet[1] & 0x40:
                        raise ValueError("遇到缺少 PES 起点的视频续包")
                    self.pending.append(packet)
                    self.pending_size += PACKET_SIZE
                    if self.pending_size > self.max_pes_bytes:
                        raise ValueError("视频 PES 超过配置的缓冲上限")
                else:
                    output.write(packet)
            self._flush(output)
        if progress:
            progress(done, total)
        if not self.stats["video_pes"] or self.stream_type not in (0x1B, 0x24):
            raise ValueError("文件中没有完整的 AVC/HEVC 视频 PES")
        # 没有分段加密标记的合法视频保持明文；最终成功仍由工作流的完整解码与帧数验证决定。
        protection = ("mixed" if self.stats["plain_pes"] else "encrypted") if self.stats["encrypted_pes"] else "clear"
        return {**self.stats, "stream_type": self.stream_type, "protection": protection,
                "descriptor_styles": dict(self.descriptor_styles)}
