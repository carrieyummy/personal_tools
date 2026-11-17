"""保持客户端播放页不变，为目标视频独立签名并读取官方节目与媒体元数据。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
from threading import Event, RLock
import time

from ..runtime import check_cancel
from .inspector import LiveInspector, evaluate_player
from .native import native_signature, failure_status
from .session import load_session, request_mtop, response_status


class PlaybackError(ValueError):
    """已脱敏的业务错误；消息不能包含临时 URL、Cookie 或授权材料。"""


class SourceChanged(PlaybackError):
    """目标来源结构变化，旧现场保留，重新解析受配置次数限制。"""


class AuthenticationError(PlaybackError):
    """共享认证恢复失败；停止后续网络派发但保留现场。"""


# 只读快照：字段对应已验证桌面 SDK 的 getUpsParams/UPS 序列化映射。
# 不调用播放器方法，不读取有容量上限的 performance 历史，也不提取当前集签名或挑战。
CAPTURE_EXPRESSION = """(()=>{
    const c=window.videoPlayer?.context;
    if(!c?.config || !c.system || !c.upsParams || !c.drm || !c.adParams) return null;
    const u=c.upsParams;
    const gpu=window.ElectronDevice?.gpu;
    const devices=Array.isArray(gpu)?gpu:[];
    const selected=devices.find(x=>typeof x?.data==='string' && x.data.toUpperCase().includes('NVIDIA'))
        || devices.find(x=>typeof x?.data==='string' && x.data.toUpperCase().includes('AMD')) || devices[0];
    return {
        vid:c.config.vid, response_vid:c.originResource?.video?.encodeid,
        initialized:Array.isArray(c.originResource?.stream) && c.originResource.stream.length>0,
        adapter:{environment:c.config.environment, support_type:c.supportType,
                 default_support_type:c.system.defaultSupportType},
        groups:{
            steal_params:{ccode:c.config.ccode,utid:c.cna,version:c.version},
            biz_params:{h265:u.h265,brand:u.brand,appArch:u.appArch,pcdnVer:u.pcdnVer,btype:u.btype,
                chipset:selected?.data,preferClarity:u.preferClarity,app_ver:c.version,
                play_ability:u.playAbility,close_ability:u.closeAbility,
                drm_type:c.drm.drmType,key_index:c.drm.keyIndex,enc_ver:u.enc_ver,
                native_ver:c.config.electronMinorVersion},
            ad_params:{os:c.adParams.os,osv:c.adParams.osv}
        }
    };
})()"""


def captured_groups(current: dict | None, profile: dict) -> dict:
    """验证只读播放器快照 current 与专项 profile；返回新请求模板，拒绝未初始化或不兼容上下文。"""
    if (not isinstance(current, dict) or not current.get("vid") or not current.get("initialized")
            or current["vid"] != current.get("response_vid")):
        raise PlaybackError("当前播放器尚未完成正片初始化，请在优酷客户端打开一集正片后重试")
    options = profile["request"]["context"]
    if current.get("adapter") != options["adapter"]:
        raise PlaybackError("当前播放器模式不符合已验证的桌面播放适配")
    groups = deepcopy(current.get("groups", {}))
    required = {
        "steal_params": ("ccode", "utid", "version"),
        "biz_params": ("h265", "brand", "appArch", "pcdnVer", "btype", "chipset", "preferClarity",
                       "app_ver", "play_ability", "close_ability", "drm_type", "key_index", "enc_ver", "native_ver"),
        "ad_params": tuple(profile["request"]["ad_fields"]),
    }
    for group, keys in required.items():
        if not isinstance(groups.get(group), dict) or any(groups[group].get(k) in (None, "") for k in keys):
            raise PlaybackError("当前播放器缺少必要的设备或平台上下文字段")
    if groups["steal_params"]["ccode"] != profile["native"]["ccode"]:
        raise PlaybackError("当前播放器平台与原生签名配置不一致")
    groups["steal_params"]["client_ip"] = profile["native"]["client_ip"]
    groups["biz_params"] = {**options["biz_params"], **groups["biz_params"]}
    return groups


def find_main_process(profile: dict) -> int:
    """只读枚举 profile 对应的唯一优酷主进程；返回 PID，零个或多个均明确拒绝。"""
    command = "Get-CimInstance Win32_Process -Filter \"Name='YOUKU.exe'\" | Where-Object {$_.CommandLine -notmatch '--type='} | Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress"
    result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True,
                            timeout=profile["live"]["timeout_seconds"], check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise PlaybackError("无法枚举优酷主进程")
    rows = json.loads(result.stdout.decode("utf-8-sig") or "[]")
    rows = rows if isinstance(rows, list) else [rows]
    expected = (profile["client_dir"] / profile["native"]["executable"]).resolve()
    matches = [row["ProcessId"] for row in rows if row.get("ExecutablePath") and Path(row["ExecutablePath"]).resolve() == expected]
    if len(matches) != 1:
        raise PlaybackError("需要唯一已登录并初始化播放器的优酷主进程")
    return matches[0]


class Client:
    """共享只读上下文与认证快照；取源串行保护，媒体传输不持有此锁。"""

    def __init__(self, config: dict, cancel: Event):
        """以 config 配置及 cancel 取消信号初始化客户端；捕获一次当前页，不切换播放状态。"""
        self.config, self.profile, self.cancel = config, config["playback"], cancel
        self.lock, self.stopped = RLock(), Event()
        self.recoveries, self.generation, self.account = 0, 0, None
        for path, expected in ((self.profile["native"]["module"],self.profile["restoration"]["challenge_module_sha256"]),
                               (self.profile["restoration"]["library"],self.profile["restoration"]["library_sha256"])):
            if hashlib.sha256((self.profile["client_dir"] / path).read_bytes()).hexdigest() != expected:
                raise PlaybackError("客户端模块摘要不匹配，禁止加载未验证的原生版本")
        self.pid = find_main_process(self.profile)
        self.capture()
        self.jar = load_session(self.profile)
        self.loaded = time.monotonic()

    def capture(self) -> None:
        """读取已初始化播放器的实时设备与平台上下文；无参数或返回值，不依赖历史请求、不改变播放状态。"""
        with LiveInspector(self.profile, self.pid) as inspector:
            self.context = inspector.evaluate("({ua:global.userAgent,utid:global.UTDID})")
            current = evaluate_player(self.profile, inspector, CAPTURE_EXPRESSION)
        self.groups = captured_groups(current, self.profile)
        self.initial_vid = current["vid"]
        package = json.loads((self.profile["client_dir"] / "resources/app/package.json").read_text(encoding="utf-8"))
        self.version = f"{package['version']}.{package['buildnumber']}"

    def request(self, api: dict, payload: dict) -> dict:
        """按 api/payload 发起有界 MTop 请求；返回内存响应，共享认证恢复结果，不记录原始内容。"""
        with self.lock:
            check_cancel(self.cancel)
            if self.stopped.is_set():
                raise AuthenticationError("认证恢复失败，已停止新请求")
            options = self.config["authentication"]
            if time.monotonic() - self.loaded >= options["refresh_interval_seconds"]:
                self.jar = load_session(self.profile)
                self.loaded = time.monotonic()
                self.generation += 1
            for attempt in range(options["recovery_attempts"] + 1):
                result = request_mtop(self.profile, self.jar, api, payload, self.context["ua"])
                status = response_status(result)
                if status["ret_codes"] == ["SUCCESS"]:
                    return result
                rejected = set(status["ret_codes"]) & set(self.profile["authentication"]["rejected_codes"])
                if not rejected:
                    raise PlaybackError("优酷接口失败：" + json.dumps(status, ensure_ascii=False))
                if attempt == options["recovery_attempts"]:
                    self.stopped.set()
                    raise AuthenticationError("优酷认证恢复次数耗尽，请在客户端确认登录")
                # 过期响应可下发新 MTop token；保留内存 Jar，下一次请求重新签名。
                # 非 token 拒绝则只读重载登录态，不接管客户端登录。
                if not any("TOKEN" in code for code in rejected):
                    self.jar = load_session(self.profile)
                self.loaded = time.monotonic()
                self.recoveries += 1
                self.generation += 1
            raise AssertionError("不可达的认证重试状态")

    def ups(self, vid: str) -> tuple[dict, str]:
        """为目标 vid 生成新签名并请求完整正片；返回响应体及匹配挑战，二者禁止原样落盘。"""
        with self.lock:
            check_cancel(self.cancel)
            options = deepcopy(self.profile)
            options["sample"] = {"url": "https://v.youku.com/video?vid=" + vid}
            signature = native_signature(options)
            if (signature.get("returncode") != 0 or not signature.get("ckey")
                    or not signature.get("encryptR1") or not signature.get("client_ts")):
                raise PlaybackError("原生请求签名失败：" + failure_status(signature))
            if signature.get("module_sha256") != self.profile["restoration"]["challenge_module_sha256"]:
                raise PlaybackError("客户端签名模块版本与已验证适配不一致")
            steal = dict(self.groups["steal_params"], vid=vid, ckey=signature["ckey"], client_ts=int(signature["client_ts"]))
            biz = {k:v for k,v in self.groups["biz_params"].items() if k not in self.profile["request"]["remove_biz_fields"]}
            biz.update(vid=vid, encryptR_client=signature["encryptR1"])
            ads = {k:self.groups["ad_params"][k] for k in self.profile["request"]["ad_fields"] if k in self.groups["ad_params"]}
            response = self.request(self.profile["network"]["ups"],
                {k:json.dumps(v,separators=(",",":")) for k,v in (("steal_params",steal),("biz_params",biz),("ad_params",ads))})
            body = response.get("data", {}).get("data", {})
            if body.get("video", {}).get("encodeid") != vid or not body.get("stream"):
                raise PlaybackError("UPS 未返回目标视频完整媒体")
            if body.get("error"):
                raise PlaybackError("UPS 返回业务错误")
            user = body.get("user", {})
            account = user.get("uid") or user.get("userid")
            if self.account is not None and account != self.account:
                self.stopped.set()
                raise AuthenticationError("客户端账号发生变化，停止本批次")
            if account is not None:
                self.account = account
            return body, signature["encryptR1"]

    def catalog(self, vid: str, show_id: str, page: int) -> dict:
        """请求 vid/show_id 的官方选集第 page 页；返回未落盘的目录模型。"""
        options = self.profile["catalog"]
        system = dict(options["system_info"], utdid=self.context["utid"], ver=self.version)
        payload = dict(options["payload"], vid=vid, show_id=show_id,
                       system_info=json.dumps(system,separators=(",",":")))
        payload.update({options["page_field"]:page})
        response = self.request(options, payload)
        return response.get("data", {}).get("model", {})

    def diagnostics(self) -> dict:
        """返回不含账号标识的认证计数；无参数，不输出凭据。"""
        return {"generation":self.generation, "recoveries":self.recoveries, "stopped":self.stopped.is_set()}
