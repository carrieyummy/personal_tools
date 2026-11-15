"""使用腾讯原生登录态和可配置的播放能力访问 getinfo；签名只存在于请求内存。"""

import json
import math
import time
from uuid import uuid4
from urllib.parse import urlencode

from .auth import load_user
from .errors import AuthenticationError, PlaybackError, AddressExpired
from .transport import Transport
from .native_sign import generate as native_key


def video_info(data: dict) -> dict:
    """提取播放响应 data 中唯一的视频对象；返回原生字段字典，结构变化明确报错。"""
    try:
        values = data["vl"]["vi"]
        if not isinstance(values, list) or len(values) != 1:
            raise ValueError("not unique")
        return values[0]
    except (KeyError, TypeError, ValueError):
        raise PlaybackError("腾讯播放响应缺少唯一视频信息") from None


def formats(data: dict, profile: dict) -> list[dict]:
    """按 profile 的数值指标对 data 的真实档位排序；返回由高到低列表，不按名称猜测档位。"""
    values = data.get("fl", {}).get("fi")
    if not isinstance(values, list) or not values:
        raise PlaybackError("播放接口没有返回画质档位")
    seen, result = {}, []
    for value in values:
        if not isinstance(value, dict) or not value.get("name") or not value.get("id"):
            raise PlaybackError("画质档位结构变化")
        for key in ("width", "height", "vfps"):
            number = value.get(key)
            if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
                raise PlaybackError(f"画质档位 {key} 缺失或非法")
        if value["id"] in seen:
            previous = seen[value["id"]]
            # 部分剧的“臻彩1080P”与 SDR 指向同一物理码流编号，不能重复计档或冒充另一份 HDR 码流。
            for key in ("width", "height", "vfps", "vcodec", "fs", "br", "audio", "audiobandwidth"):
                if key in previous and key in value and previous[key] != value[key]:
                    raise PlaybackError("同一画质编号对应相互矛盾的码流属性")
            alias = {key: value[key] for key in ("name", "sname", "resolution", "videoenh") if key in value}
            previous["aliases"].append(alias)
            if value["name"] == value.get("formatdefn") and previous["name"] != previous.get("formatdefn"):
                aliases = previous["aliases"]
                previous.clear()
                previous.update(value, aliases=aliases)
            continue
        current = dict(value)
        current["aliases"] = [{key: value[key] for key in ("name", "sname", "resolution", "videoenh") if key in value}]
        seen[value["id"]] = current
        result.append(current)
    def score(value: dict) -> tuple:
        """按数值字段为原生 value 生成排序元组；尺寸字段须真实存在，不能从营销名推断。"""
        metrics = {**value, "pixels": int(value["width"]) * int(value["height"])}
        return tuple(float(metrics.get(key, 0)) for key in profile["ranking_fields"])
    return sorted(result, key=score, reverse=True)


class NativeClient:
    """共享已验证的用户快照与原生签名；config/cancel 控制协议、网络与取消，研究入口可显式设置 signer_pid。"""

    def __init__(self, config: dict, cancel=None, *, user: dict | None = None):
        """保存 config/cancel，读取 user（None 时只读客户端）；不发网络请求，无返回值。"""
        self.config, self.profile = config, config["playback"]
        self.transport = Transport(config, cancel)
        self.user = load_user(config) if user is None else user
        self.signer_pid = None

    def raw_source(self, series_id: str, video_id: str, definition: str | None = None,
                   extra: dict | None = None) -> dict:
        """查询 series_id/video_id；definition 为档位，extra 为研究或音轨附加参数；返回原生响应。"""
        settings = self.profile["playback"]
        query = {**settings["query"], **(extra or {})}
        query["defn"] = settings["initial_definition"] if definition is None else definition
        query, headers = self.signed_query(series_id, video_id, query)
        if settings["request_mode"] == "proxy":
            body = json.dumps({"buid": "vinfoad", "vinfoparam": urlencode(query)}).encode("utf-8")
            outer = json.loads(self.transport.text(settings["endpoint"],
                               {**headers, "Content-Type": "application/json"}, body))
            return json.loads(outer["vinfo"])
        return self.transport.json(settings["endpoint"], query, headers)

    def signed_query(self, series_id: str, video_id: str, values: dict) -> tuple[dict, dict]:
        """为 series_id/video_id 的 values 构造统一原生身份和签名；返回仅驻内存的查询与请求头。"""
        settings = self.profile["playback"]
        url = self.profile["catalog"]["page_template"].format(series_id=series_id, video_id=video_id)
        now = int(time.time())
        query = dict(values)
        query.update(vid=video_id, cid=series_id, ehost=url, refer=url,
                     appVer=settings["app_version"], flowid=uuid4().hex, tm=now,
                     logintoken=json.dumps(self.user["tokens"], separators=(",", ":")))
        signed = native_key(self.config, {"vid": video_id, "timestamp": now,
            "app_version": settings["app_version"], "platform": int(query["platform"]), "source": query["sdtfrom"]},
            pid=self.signer_pid, cancel=self.transport.cancel)
        query.update(cKey=signed["ckey"], guid=signed["guid"])
        cookie = "; ".join(f"{key}={value}" for key, value in self.user["tokens"].items())
        return query, {"Cookie": cookie, "Referer": url}

    def raw_key(self, series_id: str, video_id: str, filename: str, format_id: int) -> dict:
        """为已选 series_id/video_id 的精确 filename/format_id 请求授权键；返回内存原生响应。"""
        common = self.profile["playback"]["query"]
        query = {key: common[key] for key in ("platform", "encryptVer", "otype", "sdtfrom")}
        query.update(filename=filename, format=format_id)
        query, headers = self.signed_query(series_id, video_id, query)
        return self.transport.json(self.profile["key"]["endpoint"], query, headers)

    def source(self, series_id: str, video_id: str, definition: str | None = None,
               extra: dict | None = None) -> dict:
        """查询并校验 series_id/video_id 授权；definition/extra 控制同源；返回已认证响应。"""
        try:
            data = self.raw_source(series_id, video_id, definition, extra)
        except AddressExpired:
            raise AuthenticationError("腾讯播放接口拒绝登录请求，请在客户端重新登录") from None
        settings = self.profile["playback"]
        if str(data.get("code")) not in settings["success_codes"]:
            raise PlaybackError(f"腾讯播放查询失败：{data.get('em')}/{data.get('exem')}")
        if data.get("login") not in settings["authenticated_states"]:
            raise AuthenticationError("腾讯未接受当前客户端登录态，请在客户端重新登录")
        if str(video_info(data).get("vid")) != video_id:
            raise PlaybackError("播放响应的视频编号与请求不符")
        return data

    def catalog_page(self, series_id: str, video_id: str, context: str | None = None,
                     page_id: str | None = None) -> dict:
        """查询源站选集；series_id/video_id 标识剧集，context/page_id 指定后续分组；返回页面数据。"""
        settings = self.profile["catalog"]
        params = {**settings["page_params"], "cid": series_id, "vid": video_id}
        bypass = dict(settings["bypass_params"])
        scene = settings["main_scene"]
        if context is not None:
            if not page_id:
                raise PlaybackError("后续选集页缺少 page_id")
            params.update(page_type="detail_operation", page_id=page_id, page_context=context)
            bypass.update(page_type="detail_operation", page_id=page_id,
                          data_mode="default", user_mode="default", new_mark_label_enabled="1")
            scene = settings["operation_scene"]
        query = {**settings["query"], "vdevice_guid": uuid4().hex[:16]}
        body = {"page_params": params, "page_context": {},
                "page_bypass_params": {"params": bypass, "scene": scene, "app_version": "",
                                       "abtest_bypass_id": uuid4().hex}}
        text = self.transport.text(settings["endpoint"] + "?" + urlencode(query),
                                   {"Content-Type": "application/json"}, json.dumps(body).encode("utf-8"))
        data = json.loads(text)
        if data.get("ret") != 0 or not isinstance(data.get("data"), dict):
            raise PlaybackError("腾讯选集接口失败或结构变化")
        return data["data"]
