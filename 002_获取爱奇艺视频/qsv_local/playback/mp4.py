"""读取受界限约束的 MP4 结构并通过本机 SDK 处理 CENC 样本；不读取或导出授权密钥。"""

import struct


def boxes(data: bytes | bytearray, start: int = 0, end: int | None = None):
    """枚举 data 的 [start,end) 顶层 box；返回 (起点,终点,类型,正文起点)，拒绝越界或截断结构。"""
    end = len(data) if end is None else end
    position = start
    while position < end:
        if position + 8 > end:
            raise ValueError("MP4 box 头被截断")
        size, kind = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1:
            if position + 16 > end:
                raise ValueError("MP4 扩展长度被截断")
            size, header = struct.unpack_from(">Q", data, position + 8)[0], 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            raise ValueError("MP4 box 长度越界")
        yield position, position + size, kind, position + header
        position += size


def descendants(data: bytes | bytearray, start: int = 0, end: int | None = None):
    """在 data 的指定边界内递归枚举已知容器；返回 box 元组，不猜测未知 box 的内部布局。"""
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"sinf", b"schi", b"moof", b"traf", b"mvex"}
    for item in boxes(data, start, end):
        yield item
        begin, finish, kind, payload = item
        if kind in containers:
            yield from descendants(data, payload, finish)
        elif kind == b"stsd":
            if payload + 8 > finish:
                raise ValueError("MP4 样本描述头被截断")
            yield from descendants(data, payload + 8, finish)
        elif kind in (b"encv", b"enca"):
            fixed = 78 if kind == b"encv" else 28
            yield from descendants(data, payload + fixed, finish)


def initialization(data: bytes) -> tuple[bytes, dict]:
    """解析初始化 data，解除已核验样本描述中的保护标记；返回供明文封装使用的初始化数据和各轨 IV 长度。"""
    output, tracks = bytearray(data), {}
    for _, moov_end, kind, moov_payload in boxes(data):
        if kind != b"moov":
            continue
        for _, track_end, track_kind, track_payload in boxes(data, moov_payload, moov_end):
            if track_kind != b"trak":
                continue
            track_id, descriptions = None, {}
            entries = list(descendants(data, track_payload, track_end))
            for begin, end, child, payload in entries:
                if child == b"tkhd":
                    offset = 20 if data[payload] == 1 else 12
                    track_id = struct.unpack_from(">I", data, payload + offset)[0]
                elif child == b"stsd":
                    for index, (entry_start, entry_end, sample_type, entry_payload) in enumerate(boxes(data, payload + 8, end), 1):
                        iv_size, codec = None, sample_type
                        if sample_type in (b"encv", b"enca"):
                            inner = list(descendants(data, entry_payload + (78 if sample_type == b"encv" else 28), entry_end))
                            original = next((data[p:p + 4] for _, _, k, p in inner if k == b"frma"), None)
                            if original is None or len(original) != 4:
                                raise ValueError("受保护样本没有原始编码标记")
                            codec = bytes(original)
                            tenc = next((row for row in inner if row[2] == b"tenc"), None)
                            if tenc is None or tenc[3] + 24 > tenc[1]:
                                raise ValueError("CENC 轨道声明被截断")
                            p = tenc[3]
                            if data[p + 6] != 1:
                                raise ValueError("CENC 保护标记尚未适配")
                            iv_size = data[p + 7]
                            if iv_size != 8:
                                raise ValueError("当前本机 CENC 适配要求 8 字节样本 IV")
                            output[entry_start + 4:entry_start + 8] = original
                        descriptions[index] = {"iv_size": iv_size, "codec": codec.decode("ascii")}
                if child in (b"sinf", b"pssh"):
                    output[begin + 4:begin + 8] = b"free"
            if track_id is None:
                raise ValueError("MP4 初始化轨道编号缺失")
            tracks[track_id] = {"descriptions": descriptions, "default_description": 1 if len(descriptions) == 1 else None}
        for _, _, child, payload in descendants(data, moov_payload, moov_end):
            if child == b"trex":
                track_id, description = struct.unpack_from(">II", data, payload + 4)
                if track_id in tracks:
                    tracks[track_id]["default_description"] = description
    if not tracks:
        raise ValueError("MP4 初始化未包含轨道")
    return bytes(output), tracks


def fragment(data: bytes, sdk, tracks: dict, absolute_offset: int = 0) -> tuple[bytes, dict]:
    """按 tracks 描述及 sdk 授权处理媒体 data；absolute_offset 是原文件范围起点；返回等长明文片段与样本统计。"""
    output = bytearray(data)
    total_samples, encrypted_samples = 0, 0
    top = list(boxes(data))
    mdats = [(payload, end) for _, end, kind, payload in top if kind == b"mdat"]
    for moof_start, moof_end, kind, moof_payload in top:
        if kind != b"moof":
            continue
        for _, traf_end, traf_kind, traf_payload in boxes(data, moof_payload, moof_end):
            if traf_kind != b"traf":
                continue
            children = list(boxes(data, traf_payload, traf_end))
            tfhd = next((item for item in children if item[2] == b"tfhd"), None)
            if tfhd is None:
                raise ValueError("MP4 片段缺少轨道头")
            _, header_end, _, p = tfhd
            flags = int.from_bytes(data[p + 1:p + 4], "big")
            track_id = struct.unpack_from(">I", data, p + 4)[0]
            if track_id not in tracks:
                raise ValueError("MP4 片段引用未登记轨道")
            cursor, base = p + 8, moof_start
            if flags & 1:
                base = struct.unpack_from(">Q", data, cursor)[0] - absolute_offset
                cursor += 8
            description_index = tracks[track_id]["default_description"]
            if flags & 2:
                description_index = struct.unpack_from(">I", data, cursor)[0]
                cursor += 4
            if description_index not in tracks[track_id]["descriptions"]:
                raise ValueError("MP4 片段引用未登记的样本描述")
            description = tracks[track_id]["descriptions"][description_index]
            default_duration, default_size = 0, 0
            if flags & 8:
                default_duration = struct.unpack_from(">I", data, cursor)[0]
                cursor += 4
            if flags & 16:
                default_size = struct.unpack_from(">I", data, cursor)[0]
                cursor += 4
            if flags & 32:
                cursor += 4
            if cursor > header_end:
                raise ValueError("MP4 轨道头声明越界")
            samples, next_position = [], None
            for _, end, child, p in children:
                if child != b"trun":
                    continue
                run_flags = int.from_bytes(data[p + 1:p + 4], "big")
                count = struct.unpack_from(">I", data, p + 4)[0]
                cursor = p + 8
                if run_flags & 1:
                    next_position = base + struct.unpack_from(">i", data, cursor)[0]
                    cursor += 4
                if next_position is None:
                    next_position = next((a for a, _ in mdats if a > moof_end), None)
                if run_flags & 4:
                    cursor += 4
                if count > len(data) // 4:
                    raise ValueError("MP4 样本数量超过当前片段边界")
                for _ in range(count):
                    duration, size = default_duration, default_size
                    if run_flags & 256:
                        duration = struct.unpack_from(">I", data, cursor)[0]
                        cursor += 4
                    if run_flags & 512:
                        size = struct.unpack_from(">I", data, cursor)[0]
                        cursor += 4
                    cursor += 4 * bool(run_flags & 1024) + 4 * bool(run_flags & 2048)
                    if cursor > end or not size or next_position is None or not any(a <= next_position and next_position + size <= b for a, b in mdats):
                        raise ValueError("MP4 样本范围越过媒体数据")
                    samples.append((next_position, size, duration))
                    next_position += size
            total_samples += len(samples)
            senc = next((item for item in children if item[2] == b"senc"), None)
            if description["iv_size"] is None:
                if senc is not None:
                    raise ValueError("明文初始化与加密片段不一致")
                continue
            if senc is None:
                raise ValueError("受保护 MP4 样本未提供已适配的 senc 信息")
            _, senc_end, _, p = senc
            senc_flags = int.from_bytes(data[p + 1:p + 4], "big")
            count = struct.unpack_from(">I", data, p + 4)[0]
            if count != len(samples) or senc_flags & ~2:
                raise ValueError("CENC 样本数或扩展标志尚未适配")
            cursor = p + 8
            for position, size, _ in samples:
                iv = bytes(data[cursor:cursor + 8])
                cursor += 8
                subsamples = b""
                if senc_flags & 2:
                    sub_count = struct.unpack_from(">H", data, cursor)[0]
                    cursor += 2
                    subsamples = bytes(data[cursor:cursor + 6 * sub_count])
                    cursor += 6 * sub_count
                    if sum(a + b for a, b in struct.iter_unpack(">HI", subsamples)) != size:
                        raise ValueError("CENC 子样本表与样本长度不一致")
                if cursor > senc_end:
                    raise ValueError("CENC 样本信息越界")
                output[position:position + size] = sdk.decrypt_cenc(bytes(data[position:position + size]), iv, subsamples)
                encrypted_samples += 1
            if cursor != senc_end:
                raise ValueError("CENC 样本信息存在未解释字节")
            for begin, _, child, _ in children:
                if child in (b"senc", b"saiz", b"saio"):
                    output[begin + 4:begin + 8] = b"free"
    return bytes(output), {"samples": total_samples, "encrypted_samples": encrypted_samples}
