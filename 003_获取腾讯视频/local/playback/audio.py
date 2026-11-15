"""独立选择原始音质档位；客户端实时滤镜如实展示，不冒充可归档的独立音源。"""

from dataclasses import dataclass, field, replace
import json
import math
import sys
from threading import Lock
from typing import Callable

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
    """从 data/video 的音质元数据构造选项；selected 提供格式及原轨码率；返回已验证档位，不把复用轨当独立音源。"""
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
    result = [original]
    effects = data.get("afl", video.get("afl", {}))
    if effects:
        if not isinstance(effects, dict) or not isinstance(effects.get("afi"), list):
            raise PlaybackError("腾讯音效列表结构变化，不能确定音质选项")
        for value in effects["afi"]:
            if not isinstance(value, dict) or not value.get("name") or "audiofilterparam" not in value:
                raise PlaybackError("腾讯音效档位缺少稳定名称")
            if not value["audiofilterparam"]:
                result[0] = replace(original, id=str(value["name"]), name=str(value.get("show_name") or value.get("cname") or value["name"]),
                                    metadata={**original.metadata, "note": "保留源站原始音轨；客户端实时音效不写入媒体"})
                continue
            # 原生 AudioEffectInfo.filter_param 交给播放器实时处理；本工具保持无损归档。
            result.append(AudioOption(str(value["name"]), str(value.get("show_name") or value.get("cname") or value["name"]),
                                      bool(value.get("vip_limit") or value.get("lmt")), bool(value.get("authorized", False)),
                                      None, "effect", False,
                                      metadata={"note": "客户端实时音效；没有独立音源，不应用滤镜或标成 VIP 成品"}))
    identities = [(item.kind, item.id) for item in result]
    if len(set(identities)) != len(identities):
        raise PlaybackError("腾讯音质档位重复")
    return result


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
    """串行化终端音质选择；settings 提供 VIP 优先和提示阈值，同剧相同选项集合复用选择。"""

    def __init__(self, settings: dict, *, reader: Callable[[str], str] | None = None,
                 interactive: Callable[[], bool] | None = None):
        """保存 settings、可选 reader/interactive 测试接口；创建选择锁和批次缓存，无返回值。"""
        self.settings, self.lock, self.choices = settings, Lock(), {}
        self.reader = input if reader is None else reader
        self.interactive = sys.stdin.isatty if interactive is None else interactive

    def select(self, series_id: str, values: list[AudioOption]) -> AudioOption:
        """为 series_id 从 values 选择音质；复用相同档位集合，忽略原轨逐集评分差异；返回可下载项，必需交互无终端时报错。"""
        if not values:
            raise PlaybackError("接口没有返回任何音质选项")
        # 原轨的 audioscore 是逐集内容评分，变化不代表音质档位集合改变；独立音质的等级仍参与匹配。
        signature = json.dumps(sorted(((item.id, item.name, item.vip, item.authorized,
                                       None if item.kind == "original" else item.rank, item.kind, item.downloadable) for item in values),
                                      key=lambda value: (value[5], value[0])), ensure_ascii=True)
        with self.lock:
            previous = self.choices.get(series_id)
            if previous and previous[0] == signature:
                return next(item for item in values if (item.kind, item.id) == previous[1])
            available = [item for item in values if item.authorized and item.downloadable]
            if not available:
                raise PlaybackError("当前账号没有可完整获取的音质档位")
            eligible = [item for item in available if item.vip] if self.settings["prefer_vip"] else available
            if not eligible:
                eligible = available
            ranked = all(item.rank is not None for item in eligible)
            tied = ranked and sum(item.rank == max(option.rank for option in eligible) for item in eligible) > 1
            prompt = (len(values) > self.settings["prompt_above"] or previous is not None
                      or len(eligible) > 1 and (not ranked or tied))
            if prompt:
                selected = self.prompt(values)
            else:
                selected = max(eligible, key=lambda item: item.rank if item.rank is not None else 0)
            self.choices[series_id] = signature, (selected.kind, selected.id)
            return selected

    def prompt(self, values: list[AudioOption]) -> AudioOption:
        """展示普通及 VIP values 并读取编号；返回有权限的可下载项，输入 q 或中断取消批次。"""
        print("可选音质（普通与 VIP 合计）：", flush=True)
        for index, item in enumerate(values, 1):
            access = "VIP" if item.vip else "普通"
            status = "可下载" if item.authorized and item.downloadable else "不可选"
            specs = "，".join(f"{key}={value}" for key, value in item.metadata.items() if value is not None)
            print(f"  {index}. {item.name} [{access}·{status}] {specs}", flush=True)
        if not self.interactive():
            raise PlaybackError("音质需要用户选择，请在交互终端重新运行；不会自动替用户选档")
        while True:
            try:
                answer = self.reader("请选择音质编号（q 取消）：").strip()
            except EOFError:
                raise PlaybackError("没有收到音质选择，任务停止") from None
            if answer.lower() == "q":
                raise KeyboardInterrupt
            if answer.isdecimal() and 1 <= int(answer) <= len(values):
                selected = values[int(answer) - 1]
                if selected.authorized and selected.downloadable:
                    return selected
            print("请输入可下载音质的编号。", flush=True)
