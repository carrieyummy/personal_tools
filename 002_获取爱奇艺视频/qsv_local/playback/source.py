"""通过一次初始化的原生播放 SDK 为任意节目选择已声明画质；敏感资源仅在内存持有。"""

from dataclasses import dataclass, field
import json
from urllib.parse import parse_qs, urlsplit

from ..native import NativeV5
from .media import media_config
from .manifests import parse_media_manifest
from .session import capture_session, fetch_manifest, is_target_dash, response_summary


@dataclass
class Source:
    """一次资源解析结果；report 可持久化，其余字段含临时资源和会话信息，禁止记录或打印。"""

    report: dict
    payload: dict = field(repr=False)
    selected: dict = field(repr=False)
    manifest: bytes = field(repr=False)
    headers: dict = field(repr=False)


def request_source(profile: dict, video: dict, override: dict | None = None, *, require_video: bool = True) -> Source:
    """按 profile 请求 video；override 为 SDK 参数，require_video 决定是否要求唯一视频；返回安全报告及内存资源。"""
    records = capture_session(profile, video, override)
    targets = [row for row in records if row["complete"] and is_target_dash(row["url"], profile["playback"], video["tvid"])]
    if not targets:
        raise RuntimeError("缺少与目标集严格匹配的完整播放响应")
    record = targets[-1]
    # QTP 已提供完整、严格对应目标的原生响应，直接使用它，避免再次 HTTP 重放造成时效与来源差异。
    native = json.loads(record["body"].decode("utf-8-sig"))
    remote = native
    if native.get("code") != "A00000" or str(native.get("data", {}).get("tvid")) != video["tvid"]:
        raise RuntimeError("播放响应失败或响应中的视频编号与目标集不一致")
    choices = [row for row in remote.get("data", {}).get("program", {}).get("video", []) if row.get("_selected") is True]
    if require_video and len(choices) != 1:
        raise RuntimeError("原生响应未唯一确定当前播放源")
    selected = choices[0] if len(choices) == 1 else {}
    report = {"mode": "native_program_request", "visible_player_switched": False, "video": video,
              "native": response_summary(native), "resource": response_summary(remote), "resource_origin": "captured_native_response",
              "observed": [{"host": urlsplit(row["url"]).hostname, "path": urlsplit(row["url"]).path,
                            "bytes": len(row["body"]), "complete": row["complete"]} for row in records],
              "download_tasks_created": False, "media_verified": False, "highest_quality_verified": False}
    query = parse_qs(urlsplit(record["url"]).query)
    report["request_selection"] = {key: query[key] for key in ("abid", "atype", "bid", "br", "fr", "cf", "ct", "lid") if key in query}
    headers = {name.strip(): value.strip() for line in record["headers"] for name, separator, value in [line.partition(":")]
               if separator and name.strip().casefold() == "user-agent"}
    return Source(report, remote, selected, b"", headers)


def tier_key(source: dict, settings: dict) -> tuple:
    """根据原生 source 与 settings.rank_fields 计算声明画质组；返回可排序元组，缺少档位数据时报错。"""
    values = {}
    for key in ("bid", "br", "fr"):
        value = source.get(key)
        if type(value) is not int or value <= 0:
            raise ValueError("原生候选缺少有效的分辨率、码率档或帧率，不能猜测画质顺序")
        values[key] = value
    hdr = source.get("dr")
    if type(hdr) is not int:
        raise ValueError("原生候选缺少有效 HDR 类型")
    tsi = source.get("tsi", {})
    values["enhanced"] = int(source.get("zqyh") == 1 or hdr > 0 or
                             isinstance(tsi, dict) and any(tsi.get(key) == "cuva" for key in ("sdr_type", "edr_type")))
    return tuple(values[key] for key in settings["rank_fields"])


def candidate_groups(candidates: list[dict], settings: dict) -> list[list[dict]]:
    """按 settings 对 candidates 分组排序；返回从高到低的画质组，同组按 HDR、编码偏好及原生选中状态排序。"""
    groups = {}
    for item in candidates:
        groups.setdefault(tier_key(item, settings), []).append(item)

    def preference(item: dict) -> tuple:
        """返回候选 item 在同档内的偏好次序；未知类型置于已声明偏好之后，不据此降低画质组。"""
        hdrs, codecs = settings["hdr_preference"], settings["codec_preference"]
        return (not item.get("_selected", False) if settings["prefer_native_selected"] else False,
                hdrs.index(item["dr"]) if item["dr"] in hdrs else len(hdrs),
                codecs.index(item["code"]) if item["code"] in codecs else len(codecs), not item.get("_selected", False), item["vid"])

    return [sorted(groups[key], key=preference) for key in sorted(groups, reverse=True)]


def select_source(profile: dict, video: dict, rank: int) -> Source:
    """为 video 选择 profile 规则下第 rank 个原生声明画质组；拒绝试看片源、未支持格式或静默降档，返回内存资源。"""
    if type(rank) is not int or rank <= 0:
        raise ValueError("quality 必须是正整数")
    settings = profile["playback"]["selection"]
    if rank not in settings["supported_ranks"]:
        raise ValueError(f"当前链接适配仅验证了 quality={settings['supported_ranks']}；其他排名的精确选档尚未适配")
    result = request_source(profile, video)
    try:
        return validate_selected_source(profile, video, rank, result)
    except Exception as error:
        error.report = result.report
        raise


def validate_selected_source(profile: dict, video: dict, rank: int, result: Source) -> Source:
    """核验 result 对 video 的第 rank 档片源；profile 提供适配边界，返回已校验且持有清单的资源。"""
    settings = profile["playback"]["selection"]
    groups = candidate_groups(result.payload["data"]["program"]["video"], settings)
    if rank > len(groups):
        raise ValueError("目标视频没有所配置排名的画质组，不自动降档")
    desired = groups[rank - 1][0]
    if desired["code"] not in settings["supported_codecs"] or desired["ff"] not in settings["supported_formats"]:
        raise ValueError("所选画质组的原生源格式尚未支持，不自动降低画质")
    if desired["vid"] != result.selected["vid"]:
        override = {"bitstream": desired["bid"], "hdr_type": desired["dr"] if desired["dr"] >= 0 else settings["sdr_request_hdr"],
                    "frame_rate": desired["fr"], "bitstream_extend_info": {"bitrate_level": desired["br"], "vid": desired["vid"],
                                                                          "ff": desired["ff"], "mver": desired["mver"]}}
        result = request_source(profile, video, override)
    selected = result.selected
    if selected["vid"] != desired["vid"] or tier_key(selected, settings) != tier_key(desired, settings):
        keys = ("vid", "bid", "dr", "br", "fr", "code", "ff")
        wanted, actual = ({key: item.get(key) for key in keys} for item in (desired, selected))
        raise RuntimeError(f"原生 SDK 未返回所选画质来源，不接受替代源或降档；请求 {wanted}，实际 {actual}")
    drm = selected.get("drm", {})
    if drm.get("code") != "A00000" or drm.get("preview") is not False or not isinstance(drm.get("ticket"), str):
        raise RuntimeError("所选画质未取得完整授权，不将试看当成完整正片")
    result.manifest, result.report["manifest"] = fetch_manifest(profile, selected)
    plan = parse_media_manifest(result.manifest, selected, profile)
    if abs(plan["duration"] - video["duration_seconds"]) > profile["playback"]["media_download"]["duration_tolerance_seconds"]:
        raise RuntimeError("所选画质的播放清单不是完整正片")
    with NativeV5(media_config(profile)["sdk"], drm["ticket"].encode("ascii")):
        pass
    result.report["selection"] = {"rank": rank, "available_declared_groups": len(groups), "rank_fields": settings["rank_fields"],
                                   "tier": list(tier_key(selected, settings)), "source_vid": selected["vid"],
                                   "native_selection_verified": True, "scope": "native_returned_quality_groups"}
    return result


def select_av_source(profile: dict, video: dict, video_rank: int, audio_rank: int) -> Source:
    """按 profile 独立选择 video 的 video_rank 视频和 audio_rank 音轨；返回合并内存计划及安全选档证明，不改变播放器。"""
    from .audio import audio_segments, select_native_audio
    result = select_source(profile, video, video_rank)
    try:
        audio = select_native_audio(profile, video, result, audio_rank)
        result.report["audio_selection"] = audio.report
        # 语言名称来自最初视频响应中与实际选定 AID 一致的音轨，避免重新请求时的展示字段缺失。
        audio_rows = result.payload.get("data", {}).get("program", {}).get("audio", [])
        language = next((row.get("name") for row in audio_rows
                         if row.get("aid") == audio.report["source_aid"] and row.get("lid") == result.selected.get("lid")), None)
        dispatch = audio.payload["data"].get("dd")
        if not isinstance(dispatch, str) or not dispatch:
            raise RuntimeError("原生音轨响应缺少调度前缀")
        result.selected = {**result.selected, "_audio_selection": audio.report, "_audio_language": language,
                           "_audio_segments": audio_segments(profile, audio.selected, video["duration_seconds"], dispatch)}
        return result
    except Exception as error:
        error.report = result.report
        raise
