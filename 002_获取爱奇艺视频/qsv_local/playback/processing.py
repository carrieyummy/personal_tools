"""将已校验的分段按实际容器还原为独立音视频输入，供无损封装使用。"""

from pathlib import Path
import gzip
from io import BytesIO

from ..native import NativeV5
from ..runtime import check_cancel
from ..transport import TransportDecoder
from .mp4 import fragment, initialization


def process_audio_assets(profile: dict, config: dict, paths: list[Path], work: Path, controls, progress) -> Path:
    """按原生 AMP4 顺序处理 paths；profile/config 限制头部解压及复制，controls 响应取消，progress 上报进度；返回完整音轨。"""
    if not paths:
        raise ValueError("独立音轨没有已下载资源")
    target = work / "highest_audio.m4a"
    done, total = 0, sum(path.stat().st_size for path in paths)
    with target.open("xb") as output:
        for index, path in enumerate(paths):
            check_cancel(controls.cancel)
            with path.open("rb") as source:
                prefix = source.read(2)
                source.seek(0)
                if index == 0 and prefix == b"\x1f\x8b":
                    maximum = profile["playback"]["media_download"]["max_fragment_bytes"]
                    with gzip.GzipFile(fileobj=source) as header:
                        data = header.read(maximum + 1)
                    if len(data) > maximum or data[4:8] != b"ftyp":
                        raise ValueError("AMP4 初始化内容无效或超过配置上限")
                    output.write(data)
                else:
                    while chunk := source.read(config["copy_buffer_bytes"]):
                        check_cancel(controls.cancel)
                        if index == 0 and output.tell() == 0 and chunk[4:8] != b"ftyp":
                            raise ValueError("AMP4 首段不是已适配的初始化")
                        output.write(chunk)
            done += path.stat().st_size
            progress(done, total)
    return target


def process_hls(config: dict, source: dict, segments: list[dict], paths: list[Path], work: Path, controls, progress):
    """按原始顺序拼接 paths 对应的 HLS segments；使用 config/source 授权，controls 限制 SDK，progress 上报字节；返回输入及证明。"""
    outer, clear = work / "outer.ts", work / "clear.ts"
    with outer.open("xb") as output:
        for path in paths:
            with path.open("rb") as stream:
                while chunk := stream.read(config["copy_buffer_bytes"]):
                    check_cancel(controls.cancel)
                    output.write(chunk)
    with controls.slot("sdk"), NativeV5(config["sdk"], source["drm"]["ticket"].encode("ascii")) as sdk:
        proof = TransportDecoder(sdk, config["max_pes_bytes"]).convert(outer, clear, progress, config["copy_buffer_bytes"])
    proof["container"] = "ts"
    proof["video_codec"] = "hevc" if proof["stream_type"] == 0x24 else "h264"
    return [clear], proof


def process_dash(profile: dict, config: dict, source: dict, plan: dict, paths: list[Path], work: Path, controls, progress):
    """按 plan 轨道顺序处理 paths 的 MP4 初始化及 CENC 样本；profile/config 控制边界，source 提供授权；返回明文输入和证明。"""
    maximum = profile["playback"]["media_download"]["max_fragment_bytes"]
    total = sum(path.stat().st_size for path in paths)
    completed, video_samples, encrypted_samples, outputs = 0, 0, 0, []
    with controls.slot("sdk"), NativeV5(config["sdk"], source["drm"]["ticket"].encode("ascii")) as sdk:
        sdk.configure_cenc()
        for track_index, track in enumerate(plan["tracks"]):
            target = work / f"clear_{track_index:02d}_{track['kind']}.mp4"
            outputs.append(target)
            track_info = None
            with target.open("xb") as output:
                for row, path in zip(plan["segments"], paths):
                    if row["track"] != track_index:
                        continue
                    check_cancel(controls.cancel)
                    if path.stat().st_size > maximum:
                        raise ValueError("DASH 分片超过配置的单片结构处理内存上限")
                    raw = path.read_bytes()
                    if row["initialization"]:
                        if track_info is not None:
                            raise ValueError("DASH 单轨出现重复初始化")
                        clear, track_info = initialization(raw)
                    else:
                        if track_info is None:
                            raise ValueError("DASH 媒体先于初始化到达")
                        clear, stats = fragment(raw, sdk, track_info, row["start"])
                        encrypted_samples += stats["encrypted_samples"]
                        if track["kind"] == "video":
                            video_samples += stats["samples"]
                    output.write(clear)
                    completed += len(raw)
                    progress(completed, total)
    video = next(track for track in plan["tracks"] if track["kind"] == "video")
    codec = video["codecs"].split(".", 1)[0]
    return outputs, {"container": "mp4", "video_codec": {"av01": "av1", "hvc1": "hevc", "hev1": "hevc", "avc1": "h264"}.get(codec, codec),
                     "video_samples": video_samples, "encrypted_samples": encrypted_samples, "tracks": plan["tracks"]}
