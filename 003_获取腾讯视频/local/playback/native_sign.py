"""通过已运行且 ABI 已核对的腾讯播放器生成原生 cKey；参数/签名只在内存，不改变客户端配置或播放请求。"""

import hashlib
import json
from pathlib import Path
import secrets
from threading import Lock

import frida
import psutil

from ..runtime import TaskCancelled, check_cancel
from .errors import PlaybackError


_SIGNING_LOCK = Lock()


def playback_extra(settings: dict) -> dict:
    """复制 settings 的协议能力声明并生成一次性会话种子；返回内存参数，不复用客户端种子。"""
    extra = dict(settings["playback_extra"])
    extra[settings["session_seed_field"]] = str(secrets.randbits(settings["session_seed_bits"]))
    return extra


def verified_process(config: dict, settings: dict, pid: int | None = None) -> int:
    """按 config 安装目录及 settings 模块摘要核对可选 pid；未指定时须唯一匹配，返回已验证进程编号。"""
    candidates = []
    processes = [psutil.Process(pid)] if pid is not None else psutil.process_iter(["pid", "name", "exe"])
    for process in processes:
        try:
            if process.name() not in settings["process_names"] or not Path(process.exe()).resolve().is_relative_to(config["playback"]["native"]["dll_root"]):
                continue
            paths = {Path(value.path) for value in process.memory_maps(grouped=True)
                     if Path(value.path).name.lower() == settings["module"].lower()}
            if len(paths) != 1:
                continue
            if hashlib.sha256(next(iter(paths)).read_bytes()).hexdigest() != settings["sha256"]:
                raise PlaybackError("运行中播放器签名 DLL 未验证；请运行 research/verify_native_signing.py 验收候选 ABI")
            context = settings["context"]
            mappings = process.memory_maps(grouped=True)
            for location in [context, *context["mirrors"]]:
                context_paths = {Path(value.path) for value in mappings if Path(value.path).name.lower() == location["module"].lower()}
                if len(context_paths) != 1 or hashlib.sha256(next(iter(context_paths)).read_bytes()).hexdigest() != location["sha256"]:
                    raise PlaybackError("运行中播放器设备上下文 ABI 未验证；请运行 research/verify_native_signing.py 验收")
            candidates.append(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if len(candidates) != 1:
        raise PlaybackError("需要唯一且 SDK 已就绪的腾讯播放器；请在客户端播放一集已授权正片，并关闭多余播放器")
    return candidates[0]


def generate(config: dict, request: dict, *, pid: int | None = None, cancel=None) -> dict:
    """按 config 在显式或唯一 pid 的 SDK 为 request 的视频/时间/平台签名；cancel 可取消；返回内存 ckey/guid 字典。"""
    settings = config["playback"]["playback"]["ckey"]
    with _SIGNING_LOCK:
        check_cancel(cancel)
        target = verified_process(config, settings, pid)
        protocol = Path(__file__).with_name("templates").joinpath("signing_protocol.js").read_text(encoding="utf-8")
        source = "const settings=" + json.dumps(settings) + ";\n" + protocol + "\n" + r'''
const module = Process.getModuleByName(settings.module);
const sign = new NativeFunction(module.getExportByName(settings.export), 'pointer',
  ['pointer','pointer','pointer','pointer','int','int','pointer','int','pointer','pointer','pointer']);
const free = new NativeFunction(module.base.add(settings.free_import_rva).readPointer(), 'void', ['pointer']);
rpc.exports.generate = function (request, extra) {
  // request 是视频/时间/平台输入，extra 为一次性能力声明；复制返回值后使用同一 CRT 释放 SDK 内存。
  const context = settings.context;
  const deviceValues = [context, ...context.mirrors].map(location => {
    // location 为已验收设备位置；每个副本都必须可读且内容一致，不能任取一个不一致的来源。
    const value = Process.getModuleByName(location.module).base.add(location.object_rva).add(location.guid_offset);
    const guidSize = value.add(16).readU64().toNumber();
    const capacity = value.add(24).readU64().toNumber();
    if (guidSize <= 0 || guidSize > context.max_guid_bytes || capacity < guidSize) throw new Error('SDK 设备上下文未初始化');
    const guid = (capacity < 16 ? value : value.readPointer()).readUtf8String(guidSize);
    if (guid.length !== guidSize) throw new Error('SDK 设备字符串长度异常');
    return guid;
  });
  const guid = consistentGuid(deviceValues);
  const values = settings.extra_values;
  const array = Memory.alloc(Math.max(4, values.length * 4));
  values.forEach((value, index) => array.add(index * 4).writeS32(value));
  const length = Memory.alloc(4);
  length.writeS32(0);
  const result = sign(Memory.allocUtf8String(request.vid), Memory.allocUtf8String(request.app_version),
    Memory.allocUtf8String(guid), Memory.allocUtf8String(request.source), request.platform, request.timestamp,
    array, values.length, Memory.allocUtf8String(JSON.stringify(extra)), Memory.allocUtf8String(''), length);
  if (result.isNull()) throw new Error('SDK 未返回签名内存');
  try {
    const size = length.readS32();
    if (size <= 0 || size > settings.max_output_bytes) throw new Error('SDK 返回长度异常');
    return {ckey:result.readUtf8String(size), guid:guid};
  } finally { free(result); }
};
'''
        session, script = None, None
        try:
            session = frida.attach(target)
            script = session.create_script(source)
            script.load()
            result = script.exports_sync.generate(request, playback_extra(settings))
            key = result.get("ckey") if isinstance(result, dict) else None
            if not isinstance(key, str) or not 0 < len(key) <= settings["max_output_bytes"] or key.lstrip("-").isdecimal():
                raise PlaybackError("原生 SDK 拒绝生成播放签名；请检查播放器登录及 SDK 初始化状态")
            device_guid = result.get("guid")
            if not isinstance(device_guid, str) or not 0 < len(device_guid) <= settings["context"]["max_guid_bytes"]:
                raise PlaybackError("原生 SDK 没有返回有效设备上下文")
            check_cancel(cancel)
            return result
        except (PlaybackError, TaskCancelled):
            raise
        except Exception:
            raise PlaybackError("原生播放签名上下文不可用；请保持客户端播放器运行") from None
        finally:
            try:
                if script is not None:
                    script.unload()
            finally:
                if session is not None:
                    session.detach()
