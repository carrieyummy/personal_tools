"""按配置排名选择可授权下载的原始音源；播放器实时滤镜不属于下载音质选项。"""

from dataclasses import dataclass, field
import math

from .errors import PlaybackError


@dataclass(frozen=True)
class AudioOption:
    """音质选项；id/name 标识档位，vip/authorized 为权限，rank 越大越高，kind 区分原轨/独立轨/实时效果。"""

    id: str
    name: str
    vip: bool
    authorized: bool
    rank: float | None
    kind: str
    downloadable: bool = True
    metadata: dict = field(default_factory=dict)
    request: dict = field(default_factory=dict)

    def describe(self) -> dict:
        """返回可落盘的音质说明；无参数；不包含请求参数、媒体 URL 或音效滤镜原文。"""
        return {"id": self.id, "name": self.name, "vip": self.vip, "authorized": self.authorized,
                "rank": self.rank, "kind": self.kind, "downloadable": self.downloadable, **self.metadata}


def options(data: dict, video: dict, selected: dict) -> list[AudioOption]:
    """从 data/video 提取可下载原始音源；selected 提供格式及原轨码率；返回已验证档位，忽略 afl 实时滤镜。"""
    original = AudioOption("original", "原始音轨", False, True, 0, "original",
                           metadata={"bitrate": selected.get("audiobandwidth"),
                                     "note": "接口未声明独立 VIP 音质；保留源站原始音轨"})
    tracks = video.get("audio_track_list", [])
    alternate = data.get("al")
    if tracks:
        raise PlaybackError("接口返回尚未验证的语言/独立音轨协议，不能猜测音质")
    if alternate:
        if not isinstance(alternate, dict) or not isinstance(alternate.get("ai"), list):
            raise PlaybackError("腾讯音轨列表结构变化")
        if alternate["ai"]:
            original = multiplexed_audio(alternate["ai"], selected)
    return [original]


def multiplexed_audio(values: list[dict], selected: dict) -> AudioOption:
    """核对 values 为 selected 视频中已选的单个复用原轨；返回真实显示名和规格，独立音源或未知结构明确拒绝。"""
    if len(values) != 1 or not isinstance(values[0], dict):
        raise PlaybackError("接口返回尚未验证的独立音轨集合，不能猜测选择参数")
    value = values[0]
    # 已验证原生协议：type=1、物理格式编号与视频相同、已选中、无独立文件/键/地址，fs=0。
    # 只把此明确描述视作视频内原轨；其他音轨协议需实测适配，不能静默丢弃。
    if (value.get("id") != selected.get("id") or value.get("type") != 1 or value.get("sl") != 1
            or value.get("lmt") != 0 or value.get("preview") != -1 or value.get("fs") != 0
            or value.get("control") != 0 or value.get("feature") != 0 or value.get("action")
            or value.get("filename") or value.get("keyid") or value.get("ul") != {"ui": []}
            or not value.get("name")):
        raise PlaybackError("接口返回尚未验证的独立音轨协议，不能忽略后继续下载或猜测音质")
    score = value.get("audioscore")
    if type(score) not in (int, float) or not math.isfinite(score):
        raise PlaybackError("复用音轨的音质评分缺失或非法")
    return AudioOption("original", str(value["name"]), False, True, float(score), "original",
        metadata={"bitrate": selected.get("audiobandwidth"), "audio_format": value.get("audio"), "track": value.get("track"),
                  "note": "所选视频内的源站原始音轨；客户端实时音效不写入媒体"})


class AudioSelector:
    """按 settings.quality 选择可下载音源的第 N 档；每集独立排名，不需要终端交互。"""

    def __init__(self, settings: dict):
        """保存含正整数 quality 的 settings；排名从 1 开始，1 表示最高可下载音质；无返回值。"""
        self.settings = settings

    def select(self, values: list[AudioOption]) -> AudioOption:
        """从 values 中按 rank 降序选择配置档位；仅原轨/独立轨且已授权可下载的项参与；返回选中音源，档位不足或无法排名时报错。"""
        available = [item for item in values if item.authorized and item.downloadable and item.kind in ("original", "track")]
        quality = self.settings["quality"]
        if len(available) < quality:
            raise PlaybackError(f"可下载的音质档位不足，无法选择第 {quality} 档")
        if len(available) > 1:
            if any(type(item.rank) not in (int, float) or not math.isfinite(item.rank) for item in available):
                raise PlaybackError("可下载音源缺少可比较的音质等级，无法按 audio.quality 排名")
            available.sort(key=lambda item: item.rank, reverse=True)
        return available[quality - 1]
