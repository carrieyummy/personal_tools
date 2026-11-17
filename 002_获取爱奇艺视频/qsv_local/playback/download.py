"""从原生播放清单获取完整 TS、授权处理、全片验证并安全发布；不接触离线下载派发接口。"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from functools import partial
import hashlib
import json
from pathlib import Path
import time
from threading import Lock
from urllib.parse import parse_qs, urlsplit

from ..process import probe_media, read_progress, run_process
from ..storage import atomic_json, fingerprint, read_json, valid_digest
from ..verification import decode_command, validate_progress
from ..runtime import check_cancel
from .coordination import SegmentCancellation, StageControls
from .media import MediaNetworkError, media_config, save_segment, signed_segment
from .manifests import selected_media_plan
from .processing import process_audio_assets, process_dash, process_hls
from .publication import publish_validated, resume_validated


def segment_ranges(segments: list[dict], declared_size: int) -> list[int]:
    """核对 segments 的物理文件范围 [start,end) 连续且不重复，合计等于 declared_size；返回各段字节数。"""
    positions, lengths = {}, []
    for item in segments:
        parsed = urlsplit(item["url"])
        query = parse_qs(parsed.query)
        if any(len(query.get(name, [])) != 1 for name in ("start", "end")):
            raise ValueError("媒体段未唯一声明起止范围")
        start, end = (int(query[name][0]) for name in ("start", "end"))
        key = (parsed.hostname, parsed.path)
        if start < 0 or end <= start or start != positions.get(key, 0):
            raise ValueError("媒体段范围缺失、重复或不连续")
        positions[key] = end
        lengths.append(end - start)
    if sum(lengths) != declared_size:
        raise ValueError(f"清单字节数 {sum(lengths)} 与播放源声明 {declared_size} 不一致")
    return lengths


def manifest_identity(profile: dict, video: dict, source: dict, segments: list[dict]) -> dict:
    """为 profile/video/source 和 segments 生成不含地址或票据的稳定来源标识；返回断点复用所需的声明及摘要。"""
    shape = [{"path_sha256": hashlib.sha256(urlsplit(row["url"]).path.encode("utf-8")).hexdigest(),
              "range": {key: parse_qs(urlsplit(row["url"]).query)[key] for key in ("start", "end")},
              "resource_index": row.get("resource_index", parse_qs(urlsplit(row["url"]).query).get("qd_index")),
              "track": row.get("track"), "audio_asset": row.get("audio_asset", False), "initialization": row.get("initialization"), "duration": row["duration"]} for row in segments]
    value = {"tvid": video["tvid"], "vid": source["vid"], "vsize": source["vsize"],
             "sdk_sha256": profile["playback"]["player"]["sha256"], "segments": shape,
             "audio_selection": source.get("_audio_selection")}
    return {"sha256": hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
            "tvid": video["tvid"], "vid": source["vid"], "segment_count": len(segments)}


def download_steps(profile: dict, video: dict, source: dict, body: bytes, work: Path, headers: dict,
                   selection: dict | None, controls: StageControls, notify=None):
    """按 profile 处理 video/source/body 到 work；headers/selection 为请求及选档，controls 限制资源，notify 报进度；让出下载和待处理阶段，返回发布报告。"""
    settings = profile["playback"]["media_download"]
    config = media_config(profile)
    stopped = controls.cancel
    checkpoint = partial(atomic_json, retry_settings=config["logging"], cancel_event=stopped)
    check_cancel(stopped)
    plan = selected_media_plan(body, source, profile)
    video_rows = [row for row in plan["segments"] if not row.get("audio_asset")]
    audio_rows = [row for row in plan["segments"] if row.get("audio_asset")]
    if abs(plan["duration"] - float(video["duration_seconds"])) > settings["duration_tolerance_seconds"]:
        raise ValueError("播放清单时长与目标正片公开时长不一致")
    if plan["format"] == "hls":
        sizes = segment_ranges(video_rows, int(source["vsize"]))
        urls = [signed_segment(row, source) for row in video_rows]
    else:
        sizes = []
        for row in video_rows:
            query = parse_qs(urlsplit(row["url"]).query)
            start, end = int(query["start"][0]), int(query["end"][0])
            if start < 0 or end <= start:
                raise ValueError("DASH 分段范围无效")
            row["start"], row["end"] = start, end
            sizes.append(end - start)
        urls = [row["url"] for row in video_rows]
    sizes.extend(row["end"] - row["start"] for row in audio_rows)
    urls.extend(row["url"] for row in audio_rows)
    ticket = source.get("drm", {}).get("ticket")
    if not isinstance(ticket, str) or not ticket:
        raise ValueError("当前播放源缺少本机授权")
    identity = manifest_identity(profile, video, source, plan["segments"])
    work.mkdir(parents=True, exist_ok=True)
    identity_path, cache_path = work / "source_identity.json", work / "segments.json"
    if identity_path.exists():
        if read_json(identity_path) != identity:
            raise RuntimeError("工作目录来源发生变化，不能复用或覆盖")
    elif any(work.iterdir()):
        raise RuntimeError("工作目录已有未登记内容，不能接管或覆盖")
    else:
        checkpoint(identity_path, identity)
    validated = resume_validated(work, identity, video, config, controls)
    if validated is not None:
        validated.update(video=video, selection=selection, audio_selection=source.get("_audio_selection"))
        if notify:
            notify({"stage": "downloading", "progress": 100.0, "current_bytes": sum(sizes), "total_bytes": sum(sizes), "detail": "已验证候选完整，复用本地媒体", "work_dir": str(work)})
            notify({"stage": "waiting_processing", "detail": "候选摘要与全片验证证明匹配，等待发布"})
        yield "ready", validated["validated_fingerprint"]["size_bytes"]
        return publish_validated(profile, config, video, source, work, validated, controls, notify)
    cached = read_json(cache_path) if cache_path.exists() else {"source_identity": identity["sha256"], "segments": {}}
    if cached.get("source_identity") != identity["sha256"] or not isinstance(cached.get("segments"), dict):
        raise RuntimeError("分段复用记录与当前来源不一致")
    records = {}
    for key, value in cached["segments"].items():
        if not key.isdecimal() or not 0 <= int(key) < len(urls) or not isinstance(value, dict) or not valid_digest(value.get("sha256")):
            raise RuntimeError("分段复用记录损坏，保留现场")
        index, path = int(key), work / f"segment_{int(key):05d}.ts"
        if path.exists():
            if not path.resolve().is_relative_to(work.resolve()):
                raise RuntimeError("缓存分段链接越出本任务目录")
            if not path.is_file():
                raise RuntimeError("缓存分段不是普通文件")
            if path.stat().st_size == sizes[index]:
                actual = fingerprint(path, config["copy_buffer_bytes"], force=True, cancel_event=stopped)
                if actual["sha256"] == value["sha256"]:
                    records[index] = actual
    retired = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    artifacts = [work / name for name in ("outer.ts", "clear.ts", "highest_audio.m4a", "audio_media.log", "validated.partial.mp4", "validated.json", "mux.log", "mux_progress.log", "media.log", "decode.log", "decode_progress.log", "full_report.json")]
    artifacts += [work / f"segment_{index:05d}.ts" for index in range(len(urls)) if index not in records]
    artifacts += list(work.glob("clear_*.mp4"))
    for path in artifacts:
        if path.exists():
            if not path.is_file() or not path.resolve().is_relative_to(work.resolve()):
                raise RuntimeError("旧中间文件不是任务目录内的普通文件")
            path.rename(path.with_name(path.name + ".retired_" + retired))
    report = {"schema_version": 1, "record_type": "playback_download", "status": "downloading", "video": video,
              "source": {key: source[key] for key in ("vid", "bid", "dr", "code", "scrsz", "duration", "vsize")},
              "segment_count": len(urls), "manifest_seconds": plan["duration"], "download_tasks_created": False,
              "visible_player_switched": False,
              "highest_quality_verified": False, "highest_audio_verified": False, "media_verified": False, "work_dir": str(work), "source_identity": identity,
              "selection": selection, "audio_selection": source.get("_audio_selection"), "reused_segments": len(records)}
    report["source"].update({key: source[key] for key in ("br", "fr", "ff", "zqyh", "tsi") if key in source})
    segment_stop, last_update = SegmentCancellation(stopped, controls.interval), [0.0]
    metrics_lock = Lock()
    current_bytes = {index: value["size_bytes"] for index, value in records.items()}
    transferred, transfer_started, last_event = [0], time.monotonic(), [float("-inf")]

    def transfer_progress(index: int, done: int, force: bool = False) -> None:
        """更新第 index 段本次尝试的 done 字节；force 强制发送整集汇总，聚合进度和实际传输速度，无返回值。"""
        with metrics_lock:
            previous = current_bytes.get(index, 0)
            transferred[0] += max(0, done - previous)
            current_bytes[index] = done
            total_done = sum(current_bytes.values())
            total = sum(sizes)
            elapsed = time.monotonic() - transfer_started
            speed = transferred[0] / elapsed if elapsed > 0 and transferred[0] else None
            report["download_progress"] = {"current_bytes": total_done, "total_bytes": total,
                                           "speed_bps": speed, "eta_seconds": (total - total_done) / speed if speed else None}
            if notify and (force or time.monotonic() - last_event[0] >= config["logging"]["progress_interval_seconds"]):
                notify({"stage": "downloading", "progress": total_done * 100 / total, "detail": "获取所选音视频媒体", "work_dir": str(work), **report["download_progress"]})
                last_event[0] = time.monotonic()

    def advance(stage: str, detail: str, force: bool = False):
        """按配置间隔输出 stage/detail；force 为真时立即输出并保存安全检查点，无返回值。"""
        now = time.monotonic()
        if force or now - last_update[0] >= settings["progress_seconds"]:
            metric = report.get("download_progress")
            shown = f"{metric['current_bytes'] * 100 / metric['total_bytes']:.1f}%" if report["status"] == "downloading" and metric else detail
            print(f"[{video['title']} 第 {video['episode']} 集][{stage}] {shown}", flush=True)
            checkpoint(work / "full_report.json", report)
            last_update[0] = now
            if notify:
                notify({"stage": report["status"], "detail": detail if report["status"] != "downloading" else "获取所选音视频媒体", "work_dir": str(work)})

    def obtain(index: int):
        """取得第 index 段并严格核对范围长度，按配置重试网络失败；返回段下标及无敏感数据指纹。"""
        for attempt in range(settings["attempts"]):
            if segment_stop.is_set():
                raise InterruptedError("媒体获取已取消")
            try:
                path = work / f"segment_{index:05d}.ts"
                transfer_progress(index, 0)
                row = plan["segments"][index]
                options = {"progress": lambda done: transfer_progress(index, done)}
                if "range_style" in row:
                    options["range_style"] = row["range_style"]
                return index, save_segment(profile, urls[index], row.get("headers", headers), path, sizes[index], config["copy_buffer_bytes"], segment_stop, **options)
            except MediaNetworkError:
                if attempt + 1 == settings["attempts"]:
                    raise
                segment_stop.wait(settings["retry_seconds"])
        raise RuntimeError("媒体获取没有产生结果")

    pool, pending = None, {}
    try:
        yield "download", 0
        check_cancel(stopped)
        transfer_progress(-1, 0)
        advance("下载", "开始下载所选音视频媒体", True)
        pool = ThreadPoolExecutor(max_workers=settings["workers"])
        next_index = 0
        while next_index < len(urls) or pending:
            while next_index < len(urls) and len(pending) < settings["workers"]:
                if next_index in records:
                    next_index += 1
                    continue
                future = pool.submit(obtain, next_index)
                pending[future] = next_index
                next_index += 1
            finished, _ = wait(pending, timeout=settings["progress_seconds"], return_when=FIRST_COMPLETED)
            for future in finished:
                pending.pop(future)
                index, result = future.result()
                records[index] = result
                checkpoint(cache_path, {"source_identity": identity["sha256"], "segments": {str(index): value for index, value in records.items()}})
            report["downloaded_segments"] = len(records)
            advance("下载", "正在获取所选音视频媒体")
        pool.shutdown(wait=True)
        pool = None
        transfer_progress(len(urls) - 1, sizes[-1], force=True)
        report["segments"] = [{"index": index, **records[index]} for index in range(len(urls))]
        report["status"] = "waiting_processing"
        advance("等待处理", "分段下载完成，等待后处理", True)
        yield "ready", sum(record["size_bytes"] for record in records.values())
        check_cancel(stopped)
        paths = [work / f"segment_{index:05d}.ts" for index in range(len(urls))]
        for index, path in enumerate(paths):
            if fingerprint(path, config["copy_buffer_bytes"], force=True, cancel_event=stopped) != records[index]:
                raise RuntimeError("媒体分段在处理前发生变化")
        report["status"] = "decrypting"
        advance("处理授权", "按原始顺序处理全部媒体分段", True)
        def conversion_progress(done: int, total: int) -> None:
            """更新 done/total 转换进度并响应批次取消；无返回值。"""
            check_cancel(stopped)
            if notify:
                notify({"stage": "decrypting", "progress": done * 100 / total, "detail": "按实际容器处理播放授权"})
            advance("处理授权", f"{done * 100 / total:.1f}%")

        media_paths, audio_paths = paths[:len(video_rows)], paths[len(video_rows):]
        if plan["format"] == "hls":
            inputs, report["conversion"] = process_hls(config, source, video_rows, media_paths, work, controls, conversion_progress)
        else:
            inputs, report["conversion"] = process_dash(profile, config, source, {**plan, "segments": video_rows}, media_paths, work, controls, conversion_progress)
        audio_index = None
        if audio_paths:
            if notify:
                notify({"stage": "restoring_audio", "detail": "重建所选完整音轨"})
            audio_path = process_audio_assets(profile, config, audio_paths, work, controls, lambda done, total: notify({"stage": "restoring_audio", "progress": done * 100 / total, "detail": "重建所选完整音轨"}) if notify else None)
            report["audio_media"] = probe_media(config, audio_path, work / "audio_media.log", cancel_event=stopped, require_video=False)
            expected_codecs = profile["playback"]["audio_selection"]["output_codecs"][source["_audio_selection"]["codec"]]
            audio_media = report["audio_media"]
            if audio_media["errors"] or len(audio_media["audio_codecs"]) != 1 or audio_media["audio_codecs"][0] not in expected_codecs or abs(audio_media["duration_seconds"] - plan["duration"]) > settings["duration_tolerance_seconds"]:
                raise RuntimeError("独立音轨的实际编码、时长或轨道数与选档声明不一致")
            audio_index = len(inputs)
            inputs.append(audio_path)
        candidate = work / "validated.partial.mp4"
        report["status"] = "muxing"
        advance("封装", "无损封装所选视频和音轨", True)
        mux_command = [str(config["ffmpeg_path"]), "-hide_banner", "-nostdin", "-v", "warning", "-n"]
        for path in inputs:
            mux_command += ["-i", str(path)]
        if plan["format"] == "hls":
            mux_command += ["-map", "0:v:0"]
            if audio_index is None:
                mux_command += ["-map", "0:a"]
        else:
            video_index = next(i for i, track in enumerate(plan["tracks"]) if track["kind"] == "video")
            mux_command += ["-map", f"{video_index}:v:0"]
            for i, track in enumerate(plan["tracks"]):
                if track["kind"] == "audio":
                    mux_command += ["-map", f"{i}:a:0"]
        if audio_index is not None:
            mux_command += ["-map", f"{audio_index}:a:0"]
        mux_progress = work / "mux_progress.log"
        mux_command += ["-c", "copy", "-tag:v", {"hevc": "hvc1", "h264": "avc1", "av1": "av01"}[report["conversion"]["video_codec"]],
                        "-movflags", "+faststart", "-progress", str(mux_progress), str(candidate)]

        def mux_update(values: dict) -> None:
            """将 FFmpeg values 的媒体时间换算为本阶段进度；仅上报实际值，无返回值。"""
            if notify and values.get("out_time_us", "N/A") != "N/A":
                seconds = max(0, int(values.get("out_time_us", 0))) / 1000000
                notify({"stage": "muxing", "progress": min(99.9, seconds * 100 / plan["duration"]), "detail": "保持原编码封装音视频"})

        with controls.slot("mux"):
            run_process(mux_command, work / "mux.log", config, mux_update, mux_progress, cancel_event=stopped)
            report["media"] = probe_media(config, candidate, work / "media.log", cancel_event=stopped)
        media = report["media"]
        if (f"{media['width']}x{media['height']}" != source["scrsz"] or media["errors"]
                or abs(media["duration_seconds"] - plan["duration"]) > settings["duration_tolerance_seconds"]):
            raise RuntimeError("成品媒体属性与原生播放源或完整清单不一致")
        if audio_index is not None and (media["audio_codecs"] != report["audio_media"]["audio_codecs"] or media["audio_layouts"] != report["audio_media"]["audio_layouts"]):
            raise RuntimeError("成品音轨与已验证的所选独立音轨不一致")
        report["status"] = "verifying"
        expected_frames = report["conversion"]["video_pes"] if plan["format"] == "hls" else report["conversion"]["video_samples"]

        def verification_update(values: dict) -> None:
            """根据 values 的已解码帧数上报完整验证进度；无返回值。"""
            frames = int(values.get("frame", 0))
            if notify:
                notify({"stage": "verifying", "progress": min(99.9, frames * 100 / expected_frames), "detail": "完整解码视频及全部所选音轨"})
            advance("全片验证", f"已验证 {frames} 帧")

        with controls.device() as device:
            if notify:
                notify({"stage": "verifying", "device": device, "detail": "完整解码视频及全部所选音轨"})
            advance("全片验证", f"使用 {device.upper()} 解码全部视频及音轨", True)
            progress = work / "decode_progress.log"
            run_process(decode_command(config, candidate, progress, device), work / "decode.log", config,
                        verification_update, progress,
                        cancel_event=stopped)
        with controls.slot("check"):
            report["status"] = "checking"
            advance("检查", "核对帧数和成品内容摘要", True)
            report["verification"] = {**validate_progress(read_progress(progress), expected_frames, expected_kind="视频 PES" if plan["format"] == "hls" else "MP4 视频样本"), "device": device}
            digest = fingerprint(candidate, config["copy_buffer_bytes"], force=True, cancel_event=stopped)
        report["media_verified"] = True
        report["highest_quality_verified"] = bool(selection and selection["rank"] == 1 and selection["native_selection_verified"])
        report["highest_audio_verified"] = bool(audio_index is not None and source["_audio_selection"]["rank"] == 1 and source["_audio_selection"]["native_selection_verified"])
        report.update(status="validated", validated_fingerprint=digest)
        checkpoint(work / "validated.json", report)
        report = publish_validated(profile, config, video, source, work, report, controls, notify)
        advance("完成", report["output"], True)
        return report
    except BaseException as error:
        segment_stop.set()
        report.update(status="interrupted" if isinstance(error, (KeyboardInterrupt, GeneratorExit)) or stopped.is_set() else "failed", error=str(error))
        raise
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        atomic_json(work / "full_report.json", report, retry_settings=config["logging"])


def download_full(profile: dict, video: dict, source: dict, body: bytes, work: Path, headers: dict,
                  selection: dict | None = None) -> dict:
    """按 profile 完整处理 video/source/body；work 保存现场，headers 为请求头，selection 为选档证明；返回发布报告。"""
    steps = download_steps(profile, video, source, body, work, headers, selection, StageControls(media_config(profile)))
    try:
        while True:
            try:
                next(steps)
            except StopIteration as result:
                return result.value
    finally:
        steps.close()
