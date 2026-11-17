"""按本机 SDK 的 packet 处理顺序还原 MPEG-TS PES 负载，保留 TS/PES 头与尾部非整块数据。"""

from collections import Counter
import hashlib
import mmap
from pathlib import Path

from Crypto.Cipher import AES


def decrypt_transport(data: bytes, key: bytes) -> tuple[bytes, dict]:
    """处理完整 TS 分段；data 须为 188 字节对齐数据，key 为仅驻内存的 16 字节会话密钥；返回还原副本与计数。"""
    output = bytearray(data)
    counts = _decrypt_buffer(output, key, {}, len(data), None)
    return bytes(output), counts


def _decrypt_buffer(output, key: bytes, boundaries: dict, max_pes_bytes: int, cancel) -> dict:
    """原位处理可变字节缓冲 output；key 为媒体密钥，boundaries 指明段起点和保护状态，max_pes_bytes 限制 PES；cancel 可取消，返回计数。"""
    from ..runtime import check_cancel
    if len(key) != 16 or not len(output) or len(output) % 188:
        raise ValueError("InvalidTransportOrKeyLength")
    pending = {}
    pending_sizes = Counter()
    pending_clear = {}
    continuity = {}
    counts = Counter()
    cipher = AES.new(key, AES.MODE_ECB)
    clear, discontinuity, first_in_segment = False, None, set()

    def flush(pid: int) -> None:
        """还原并写回一个 PID 的完整 PES；pid 为传输流标识；无返回值，错误结构明确失败。"""
        spans = pending.pop(pid, [])
        if not spans:
            return
        packet = b"".join(output[offset:offset + size] for offset, size in spans)
        pending_sizes.pop(pid, None)
        if len(packet) < 9 or packet[:3] != b"\0\0\1":
            raise ValueError("InvalidPesHeader")
        stream_id = packet[3]
        if not 0xC0 <= stream_id <= 0xEF:
            raise ValueError("UnsupportedPesStream")
        declared = int.from_bytes(packet[4:6], "big")
        end = 6 + declared if declared else len(packet)
        start = 9 + packet[8]
        if not start <= end <= len(packet):
            raise ValueError("TruncatedPesPacket")
        aligned_end = start + (end - start) // 16 * 16
        is_clear = pending_clear.pop(pid)
        restored = packet if is_clear else packet[:start] + cipher.decrypt(packet[start:aligned_end]) + packet[aligned_end:]
        cursor = 0
        for offset, size in spans:
            output[offset:offset + size] = restored[cursor:cursor + size]
            cursor += size
        counts["video_pes" if stream_id >= 0xE0 else "audio_pes"] += 1
        counts["decrypted_bytes"] += 0 if is_clear else aligned_end - start

    for offset in range(0, len(output), 188):
        if offset % (188 * 4096) == 0:
            check_cancel(cancel)
        if offset in boundaries:
            state = boundaries[offset]
            next_clear = state["clear"]
            if next_clear != clear or (discontinuity is not None and discontinuity != state["discontinuity"]):
                for pid in list(pending):
                    flush(pid)
                continuity.clear()
            clear, discontinuity = next_clear, state["discontinuity"]
            first_in_segment.clear()
        packet = output[offset:offset + 188]
        if packet[0] != 0x47 or packet[1] & 0x80 or packet[3] & 0xC0:
            raise ValueError("InvalidTransportPacket")
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        control = (packet[3] >> 4) & 3
        if control == 0:
            raise ValueError("InvalidAdaptationControl")
        position = 4
        if control & 2:
            position += 1 + packet[4]
        if position > 188:
            raise ValueError("InvalidAdaptationLength")
        if not control & 1 or position == 188:
            continue
        start = bool(packet[1] & 0x40)
        if start:
            flush(pid)
            if packet[position:position + 3] == b"\0\0\1":
                pending[pid] = []
                pending_clear[pid] = clear
        if pid not in pending:
            continue
        cc = packet[3] & 0x0F
        if boundaries and pid not in first_in_segment and start:
            continuity.pop(pid, None)
        first_in_segment.add(pid)
        if pid in continuity and cc != (continuity[pid] + 1) % 16:
            raise ValueError("TransportContinuityGap")
        continuity[pid] = cc
        pending[pid].append((offset + position, 188 - position))
        pending_sizes[pid] += 188 - position
        if pending_sizes[pid] > max_pes_bytes:
            raise ValueError("PesBufferLimitExceeded")
    for pid in list(pending):
        flush(pid)
    if not counts["video_pes"] or not counts["audio_pes"]:
        raise ValueError("MissingAudioOrVideoPes")
    return dict(counts)


def restore_files(paths: list[Path], segments: list[dict], records: dict, key: bytes,
                  target: Path, buffer_bytes: int, max_pes_bytes: int, cancel) -> dict:
    """校验并复制 paths 后原位还原至新 target；segments/records 提供结构和摘要，key 仅驻内存；返回 PES 计数，支持跨段 PES。"""
    from ..runtime import check_cancel
    boundaries, offset = {}, 0
    with target.open("xb") as output:
        for index, (path, segment) in enumerate(zip(paths, segments, strict=True)):
            check_cancel(cancel)
            boundaries[offset] = {"clear":segment["private"].get("DRM_NOTENCRYPT") == 1,
                                  "discontinuity":segment["discontinuity"]}
            digest, size = hashlib.sha256(), 0
            with path.open("rb") as source:
                while data := source.read(buffer_bytes):
                    check_cancel(cancel)
                    output.write(data)
                    digest.update(data)
                    size += len(data)
            record = records[str(index)]
            if size != record["bytes"] or digest.hexdigest() != record["sha256"] or size % 188:
                raise ValueError("SourceSegmentChanged")
            offset += size
    with target.open("r+b") as stream, mmap.mmap(stream.fileno(), 0) as mapped:
        result = _decrypt_buffer(mapped, key, boundaries, max_pes_bytes, cancel)
        mapped.flush()
    return result


def normalize_continuity(path: Path, cancel) -> None:
    """为本地拼接 TS path 重排各 PID 的传输连续计数；cancel 可取消，仅改 TS 头低四位，保留 PES/码流/时间戳。"""
    from ..runtime import check_cancel
    counters = {}
    with path.open("r+b") as stream, mmap.mmap(stream.fileno(), 0) as data:
        if len(data) % 188:
            raise ValueError("UnalignedTransport")
        for offset in range(0,len(data),188):
            if offset % (188*4096) == 0:
                check_cancel(cancel)
            if data[offset] != 0x47:
                raise ValueError("InvalidTransportSync")
            pid = ((data[offset+1]&31)<<8)|data[offset+2]
            header = data[offset+3]
            if header & 0x10:
                counters[pid] = (counters.get(pid,-1)+1)%16
            data[offset+3] = (header&0xF0)|counters.get(pid,0)
        data.flush()
