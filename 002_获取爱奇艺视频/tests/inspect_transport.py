"""只读检查失败样本的 TS 标记及短片解码，不输出任何票据、描述符或密钥。"""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.configuration import read_config
from qsv_local.transport import PACKET_SIZE, TransportDecoder, section_payload


def marker_shape(section: bytes) -> dict:
    """仅描述 section 中标记的位置、分隔符和字段长度；返回脱敏结构，不返回内容字段。"""
    offset = section.lower().index(b"mdcm")
    position, location = 11, "other"
    descriptor = b""
    while position + 5 <= len(section) - 4:
        end = position + 5 + ((section[position + 3] & 15) << 8) + section[position + 4]
        cursor = position + 5
        while cursor + 2 <= end:
            tag, size = section[cursor:cursor + 2]
            data = section[cursor + 2:cursor + 2 + size]
            if tag == 0x48 and len(data) >= 3:
                provider_length = data[1]
                name_offset = 2 + provider_length
                for label, value in (("provider", data[2:name_offset]),
                                     ("name", data[name_offset + 1:name_offset + 1 + data[name_offset]])):
                    if b"mdcm" in value.lower():
                        location, descriptor = label, value
            cursor += 2 + size
        position = end
    return {"section_offset": offset, "field": location, "starts_mdcm_pipe": descriptor.startswith(b"mdcm|"),
            "ends_pipe": descriptor.endswith(b"|"), "has_gv": b"gv" in descriptor.lower(),
            "field_lengths": [len(value) for value in descriptor.split(b"|")]}


def main() -> int:
    """读取 --config/--path 并采样 TS，--seconds 控制短片验证时长；输出结构统计与 FFmpeg 结果，返回解码退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="项目配置 YAML")
    parser.add_argument("--path", type=Path, required=True, help="已有失败现场的 TS 文件")
    parser.add_argument("--seconds", type=float, required=True, help="短片解码验证秒数，必须大于零")
    parser.add_argument("--full-scan", action="store_true", help="只读扫描全部 TS 包的标记，仍仅短片解码")
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("--seconds 必须大于零")
    config = read_config(args.config)
    decoder = TransportDecoder(None, config["max_pes_bytes"])
    counters = Counter()
    shapes = []
    with args.path.open("rb") as stream:
        # 块长对齐 TS 包边界，避免跨块丢失包头；配置仅决定内存规模。
        block = max(PACKET_SIZE, config["copy_buffer_bytes"] // PACKET_SIZE * PACKET_SIZE)
        while sample := stream.read(block):
            for offset in range(0, len(sample) - PACKET_SIZE + 1, PACKET_SIZE):
                packet = sample[offset:offset + PACKET_SIZE]
                pid = ((packet[1] & 31) << 8) + packet[2]
                decoder._tables(packet, pid)
                counters["packets"] += 1
                counters["scrambled_packets"] += bool(packet[3] >> 6)
                if pid == 17:
                    section = section_payload(packet)
                    counters["sdt_packets"] += 1
                    counters["mdcm_markers"] += b"|mdcm|" in section
                    counters["gv_markers"] += b"|gv|" in section
                    counters["mdcm_anywhere"] += b"mdcm" in section.lower()
                    counters["gv_anywhere"] += b"gv" in section.lower()
                    counters["pipe_sections"] += b"|" in section
                    if b"mdcm" in section.lower():
                        shape = marker_shape(section)
                        if shape not in shapes:
                            shapes.append(shape)
            if not args.full_scan:
                break
    result = subprocess.run([str(config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-v", "error",
                             "-xerror", "-err_detect", "explode", "-i", str(args.path),
                             "-t", str(args.seconds), "-map", "0:v:0", "-f", "null", "-"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=config["process_timeout_seconds"], check=False)
    print(json.dumps({"sample": dict(counters), "marker_shapes": shapes[:4],
                      "video_pid": decoder.video_pid, "stream_type": decoder.stream_type,
                      "decode_exit_code": result.returncode, "errors": result.stderr.splitlines()[:8]},
                     ensure_ascii=False), flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
