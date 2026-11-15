"""短时观察已运行客户端的 libcurl 请求，不修改参数；签名地址仅在内存，报告只记录白名单元数据。"""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from queue import Empty, Queue
import re
import sys
import time
from urllib.parse import parse_qs, urlsplit

import frida
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.configuration import resolve_path
from local.playback.hls import parse_media
from local.playback.transport import Transport, safe_message
from local.storage import atomic_json
from research.settings import load_research


def script_text(settings: dict, endpoints: list[str]) -> str:
    """将 settings 的观察边界及 endpoints 注入固定观察脚本；返回不更改调用参数的 Frida JavaScript。"""
    return "const settings=" + json.dumps({**settings, "endpoints": endpoints}) + ";\n" + r'''
const handles = new Map();
const observed = new Set();
const stats = {setopt:0, url:0, perform:0, multi_add:0, matched:0};
let count = 0;
const module = Process.getModuleByName(settings.module);
const getInfo = new NativeFunction(module.getExportByName('curl_easy_getinfo'), 'int', ['pointer', 'int', 'pointer']);
function readText(pointer) {
  // pointer 为 libcurl 调用的 UTF8 字符串地址；返回有界副本，非法地址返回空串。
  try {
    if (pointer.isNull()) return '';
    let length = 0;
    while (length < settings.max_string_bytes && pointer.add(length).readU8() !== 0) length++;
    return pointer.readUtf8String(length);
  } catch (_) { return ''; }
}
function capture(handle) {
  // handle 为已经设置参数的 easy 指针；只向内存管道发送播放 API/媒体请求，无返回值。
  const data = handles.get(handle.toString()) || {url:'',body:''};
  // 已在观察前配置的连接池句柄不会再次设置 URL，使用只读 getinfo 补足，不能因此漏掉播放请求。
  if (!data.url) {
    try {
      const address = Memory.alloc(Process.pointerSize);
      if (getInfo(handle, settings.curl_info_effective_url, address) === 0) data.url = readText(address.readPointer());
    } catch (_) {}
  }
  if (!data.url || count >= settings.max_events) return;
  const parts = data.url.split('/');
  const host = (parts[2] || '').split(':')[0].toLowerCase();
  const path = data.url.split('?')[0];
  const api = settings.endpoints.some(name => path.endsWith('/' + name));
  const media = settings.media_domains.some(name => host === name || host.endsWith('.' + name)) && /\.(m3u8|ts|mp4)$/.test(path);
  const key = data.url + data.body;
  if (observed.has(key)) return;
  observed.add(key);
  count++;
  stats.matched++;
  send({kind: api ? 'api' : media ? 'media' : 'other', url: data.url, body: data.body});
}
Interceptor.attach(module.getExportByName('curl_easy_setopt'), {
  onEnter(args) {
    stats.setopt++;
    const handle = args[0].toString();
    const option = args[1].toInt32();
    if (![10002, 10015, 10165, 60, 30120].includes(option)) return;
    const data = handles.get(handle) || {url:'',body:'',bodySize:-1,bodyPointer:null};
    if (option === 10002) { data.url = readText(args[2]); stats.url++; }
    else if (option === 60 || option === 30120) data.bodySize = args[2].toInt32();
    else data.bodyPointer = args[2];
    if (option !== 10002 && data.bodyPointer) {
      try {
        // 在调用方设置参数时立即复制，避免 SDK 在提交之后复用原始正文缓冲区。
        data.body = data.bodySize >= 0 && data.bodySize <= settings.max_string_bytes
          ? data.bodyPointer.readUtf8String(data.bodySize) : readText(data.bodyPointer);
      } catch (_) { data.body = ''; }
    }
    handles.set(handle, data);
  }
});
for (const name of ['curl_easy_perform', 'curl_multi_add_handle']) {
  Interceptor.attach(module.getExportByName(name), {
    onEnter(args) {
      stats[name === 'curl_easy_perform' ? 'perform' : 'multi_add']++;
      capture(args[name === 'curl_easy_perform' ? 0 : 1]);
    }
  });
}
Interceptor.attach(module.getExportByName('curl_easy_cleanup'), {
  onEnter(args) { handles.delete(args[0].toString()); }
});
Interceptor.attach(module.getExportByName('curl_easy_reset'), {
  onEnter(args) { handles.delete(args[0].toString()); }
});
const winhttp = Process.getModuleByName(settings.winhttp.module);
const queryOption = new NativeFunction(winhttp.getExportByName('WinHttpQueryOption'), 'int', ['pointer', 'uint', 'pointer', 'pointer']);
stats.winhttp_send = 0;
Interceptor.attach(winhttp.getExportByName('WinHttpSendRequest'), {
  onEnter(args) {
    // WinHTTP 公共 ABI：只查询当前请求 URL，复制有明确字节长度的可选正文，不改头、参数或回调。
    stats.winhttp_send++;
    if (count >= settings.max_events) return;
    try {
      const buffer = Memory.alloc(settings.max_string_bytes * 2);
      const size = Memory.alloc(4);
      size.writeU32(settings.max_string_bytes * 2);
      if (queryOption(args[0], settings.winhttp.option_url, buffer, size) === 0) return;
      const url = buffer.readUtf16String(Math.max(0, size.readU32() / 2 - 1));
      const length = args[4].toUInt32();
      const body = length > 0 && length <= settings.max_string_bytes ? args[3].readUtf8String(length) : '';
      count++;
      send({kind:'api', url:url, body:body});
    } catch (_) { send({kind:'error',message:'WinHTTP 请求元数据读取失败'}); }
  }
});
const proxyModule = Process.getModuleByName(settings.download_proxy.module);
for (const [name, spec] of Object.entries(settings.download_proxy.exports)) {
  stats[name] = 0;
  Interceptor.attach(proxyModule.getExportByName(name), {
    onEnter(args) {
      // 参数位置只读取已经核对的 C 字符串；内容通过内存发送，Python 端先脱敏再落盘。
      stats[name]++;
      if (count++ >= settings.max_events) return;
      send({kind:'native', name:name, values:spec.arguments.map(index => readText(args[index]))});
    }
  });
}
const signingModule = Process.getModuleByName(settings.signing.module);
stats.signing = 0;
Interceptor.attach(signingModule.getExportByName(settings.signing.export), {
  onEnter(args) {
    // qimei 内部按参数 7 的数量读取参数 6 的 int32 数组；只观察已核对 ABI，不调用或更改内容。
    stats.signing++;
    if (count++ >= settings.max_events) { this.event = null; return; }
    this.lengthPointer = args[settings.signing.output_length_argument];
    const extraValue = parseInt(args[settings.signing.extra_argument].toString(), 16);
    const extraPointer = args[settings.signing.extra_argument];
    const extraModule = Process.findModuleByAddress(extraPointer);
    const extraRange = Process.findRangeByAddress(extraPointer);
    this.event = {kind:'signing', strings:settings.signing.string_arguments.map(index => ({index:index, value:readText(args[index])})),
      numbers:settings.signing.number_arguments.map(index => ({index:index, value:args[index].toInt32()})),
      extra:readText(args[settings.signing.extra_argument]), extra_is_null:extraValue === 0,
      extra_is_epoch:Math.abs(Date.now() / 1000 - extraValue) < 3600,
      extra_module:extraModule ? {name:extraModule.name, rva:extraPointer.sub(extraModule.base).toString()} : null,
      extra_protection:extraRange ? extraRange.protection : null,
      extra_numbers:[],
      caller:Process.findModuleByAddress(this.returnAddress) ? {
        module:Process.findModuleByAddress(this.returnAddress).name,
        rva:this.returnAddress.sub(Process.findModuleByAddress(this.returnAddress).base).toString()} : null};
    this.extraPointer = extraPointer;
    const extraCount = args[settings.signing.extra_count_argument].toInt32();
    if (extraCount >= 0 && extraCount <= settings.signing.max_extra_count && !extraPointer.isNull()) {
      try { for (let index = 0; index < extraCount; index++) this.event.extra_numbers.push(extraPointer.add(index * 4).readS32()); }
      catch (_) { this.event.extra_numbers = []; }
    }
  },
  onLeave(result) {
    if (!this.event) return;
    try {
      const length = this.lengthPointer.readS32();
      this.event.output = length > 0 && length <= settings.max_string_bytes ? result.readUtf8String(length) : '';
      this.event.output_length = length;
      this.event.extra_after = readText(this.extraPointer);
      send(this.event);
    } catch (_) { send({kind:'error',message:'签名观察返回结构与已核对 ABI 不一致'}); }
  }
});
send({kind:'ready', module:module.name});
rpc.exports.summary = function () {
  // 返回已观察函数调用计数；无参数，不包含 URL、句柄或账号数据。
  return stats;
};
'''


def request_query(event: dict) -> tuple[dict, str]:
    """解析内存 event 的 URL 与代理 JSON/表单正文；返回内存参数及正文结构类型，原文不写日志。"""
    parsed = urlsplit(event["url"])
    query = parse_qs(parsed.query)
    kind = "none"
    if event.get("body"):
        text = event["body"].strip()
        if text.startswith("{"):
            try:
                body, end = json.JSONDecoder().raw_decode(text)
                kind = "json" if end == len(text) else "json_with_trailing_bytes"
                if isinstance(body, dict) and isinstance(body.get("vinfoparam"), str):
                    query.update(parse_qs(body["vinfoparam"]))
            except (ValueError, TypeError):
                kind = "invalid_json"
        else:
            kind = "form"
            query.update(parse_qs(event["body"]))
    return query, kind


def summarize_request(event: dict, fields: list[str]) -> dict:
    """从内存 event 的 API 请求提取 fields 白名单参数；返回无 URL、票据或 POST 原文的摘要。"""
    parsed = urlsplit(event["url"])
    query, kind = request_query(event)
    endpoint = parsed.path.rsplit("/", 1)[-1]
    endpoint = endpoint if re.fullmatch(r"[A-Za-z_-]{1,40}", endpoint) else "[非命名接口]"
    return {"host": parsed.hostname, "endpoint": endpoint,
            "query_names": sorted(name for name in query if re.fullmatch(r"[A-Za-z0-9_]{1,80}", name)),
            "unrecognized_name_count": sum(not bool(re.fullmatch(r"[A-Za-z0-9_]{1,80}", name)) for name in query),
            "parameters": {name: query[name] for name in fields if name in query},
            "ckey_lengths": list(map(len, query.get("cKey", []))), "body_kind": kind}


def native_arguments(values: list[str], fields: list[str]) -> tuple[list[dict], list[str]]:
    """分析内存 values 的原生 CGI/分片参数；fields 为协议白名单；返回脱敏结构及仅驻内存的 HTTP 地址。"""
    shapes, urls = [], []
    def visit(value, depth: int = 0) -> None:
        """有界展开 value 的 JSON/表单结构；depth 限制嵌套；只积累白名单值、结构及内存 URL，无返回值。"""
        if depth > 3:
            return
        if isinstance(value, dict):
            shapes.append({"type": "object", "fields": sorted(value),
                           "parameters": {name: value[name] for name in fields if name in value
                                          and isinstance(value[name], (str, int, float, bool))}})
            for nested in value.values():
                if isinstance(nested, (dict, list, str)):
                    visit(nested, depth + 1)
        elif isinstance(value, list):
            for nested in value:
                visit(nested, depth + 1)
        elif isinstance(value, str):
            text = value.strip()
            if text.startswith(("https://", "http://")):
                urls.append(text)
            elif text.startswith(("{", "[")):
                try:
                    visit(json.loads(text), depth + 1)
                except ValueError:
                    pass
            elif "=" in text:
                params = parse_qs(text)
                shapes.append({"type": "form", "fields": sorted(params),
                               "parameters": {name: params[name] for name in fields if name in params}})
    for value in values:
        visit(value)
    return shapes, list(dict.fromkeys(urls))


def signing_summary(event: dict, settings: dict, known_videos: set[str]) -> dict:
    """按 settings 将内存 event 的签名参数归类；known_videos 为当前清单编号；返回不含签名或设备标识的结构。"""
    strings = []
    json_inputs = []
    for item in event["strings"]:
        try:
            data = json.loads(item["value"])
            if isinstance(data, dict):
                json_inputs.append(data)
        except ValueError:
            pass
    for item in event["strings"]:
        value = item["value"]
        shape = {"index": item["index"], "length": len(value), "kind": "opaque"}
        if value in known_videos:
            shape.update(kind="selected_video_id", value=value)
        elif re.fullmatch(r"\d{1,3}(?:\.\d{1,6}){1,3}", value):
            shape.update(kind="version", value=value)
        elif not value:
            shape["kind"] = "empty"
        elif value.startswith(("{", "[")):
            try:
                data = json.loads(value)
                shape.update(kind="json", fields=sorted(data) if isinstance(data, dict) else [])
                if isinstance(data, dict):
                    shape["parameters"] = {name: data[name] for name in settings["public_json_fields"] if name in data
                                           and (type(data[name]) in (int, float, bool)
                                                or isinstance(data[name], str) and (data[name].isdecimal() and len(data[name]) <= 5
                                                    or name in ("apv", "cmd", "lang") and len(data[name]) <= 40))}
            except ValueError:
                pass
        strings.append(shape)
        shape["matches_json_fields"] = sorted({name for data in json_inputs for name, nested in data.items()
                                                if value and isinstance(nested, (str, int)) and str(nested) == value})
    numbers = []
    now = time.time()
    for item in event["numbers"]:
        value = item["value"]
        number = {"index": item["index"], "kind": "opaque"}
        if 0 <= value <= 65535:
            number.update(kind="flag_or_platform", value=value)
        elif abs(now - value) < 3600:
            number["kind"] = "current_epoch_seconds"
        numbers.append(number)
    output = event.get("output", "")
    output_structure = {"json_fields": [], "separator_counts": {separator: output.count(separator) for separator in ("|", ":", "\0", "\n", "&", "=", ",", ";")},
                        "hex_only": bool(re.fullmatch(r"[0-9a-fA-F]+", output)),
                        "base64_alphabet_only": bool(re.fullmatch(r"[A-Za-z0-9+/=_-]+", output)),
                        "prefix_classes": "".join("d" if value.isdecimal() else "a" if value.isalpha() else "p" for value in output[:32])}
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            output_structure["json_fields"] = sorted(parsed)
    except ValueError:
        pass
    frames = []
    # TVKPlayer 的调用方先解析两个 4 位十六进制长度，再从第 8 字节依次取两段数据；不是逐段长度前缀。
    width = settings["paired_frame_width"]
    if len(output) >= 2 * width and re.fullmatch(r"[0-9a-fA-F]+", output[:2 * width]):
        lengths = [int(output[index * width:(index + 1) * width], 16) for index in range(2)]
        if 2 * width + sum(lengths) == len(output):
            cursor, values = 2 * width, []
            for length in lengths:
                values.append(output[cursor:cursor + length])
                cursor += length
            frames.append({"layout": "paired_lengths", "width": width, "lengths": lengths,
                           "version_fields": [{"index": index, "value": value} for index, value in enumerate(values)
                                              if re.fullmatch(r"-1|\d{1,3}(?:\.\d{1,6}){1,3}", value)]})
    for width in settings["frame_widths"]:
        for offset in range(settings["frame_prefix_scan"] + 1):
            for includes_header in (False, True):
                cursor, values = offset, []
                while cursor + width <= len(output):
                    prefix = output[cursor:cursor + width]
                    if re.fullmatch(r"[0-9a-fA-F]+", prefix) is None:
                        break
                    length = int(prefix, 16) - (width if includes_header else 0)
                    cursor += width
                    if length < 0 or cursor + length > len(output):
                        break
                    values.append(output[cursor:cursor + length])
                    cursor += length
                versions = [{"index": index, "value": value} for index, value in enumerate(values)
                            if re.fullmatch(r"-1|\d{1,3}(?:\.\d{1,6}){1,3}", value)]
                if cursor == len(output) and values and versions:
                    frames.append({"width": width, "offset": offset, "includes_header": includes_header,
                                   "lengths": list(map(len, values)), "version_fields": versions})
    return {"strings": strings, "numbers": numbers, "extra_length": len(event.get("extra", "")),
            "extra_is_null": event.get("extra_is_null"), "extra_is_epoch": event.get("extra_is_epoch"),
            "extra_module": event.get("extra_module"), "extra_protection": event.get("extra_protection"),
            "extra_numbers": [number for number in event.get("extra_numbers", []) if type(number) is int and 0 <= number <= 65535],
            "extra_after_length": len(event.get("extra_after", "")), "caller": event.get("caller"),
            "output_length": event.get("output_length"), "output_structure": output_structure, "frames": frames}


def probe_media(transport: Transport, url: str, settings: dict) -> dict:
    """对内存 url 或其 HLS 首片做 settings 限定的读取；transport 为传输；返回无签名清单及媒体吞吐摘要。"""
    parsed = urlsplit(url)
    result = {"host": parsed.hostname, "extension": Path(parsed.path).suffix}
    started, count = time.monotonic(), 0
    try:
        if Path(parsed.path).suffix == ".m3u8":
            media = parse_media(transport.text(url), url)
            resource = next(value for value in media.resources if not value.initial)
            url = resource.url
            parsed = urlsplit(url)
            result.update(manifest_resources=len(media.resources), manifest_duration=media.duration,
                          media_extension=Path(parsed.path).suffix)
            # 只记录物理格式编号，授权目录与签名查询均不写入报告。
            match = re.search(r"\.f([0-9]+)(?:\.|$)", parsed.path.rsplit("/", 1)[-1])
            if match:
                result["format_id"] = int(match[1])
            started = time.monotonic()
        with transport.open(url, {"Range": f"bytes=0-{settings['sample_bytes'] - 1}"}) as response:
            result.update(status=response.code, headers={name: response.headers.get(name) for name in
                ("Content-Type", "Content-Length", "Content-Range")})
            while count < settings["sample_bytes"]:
                if time.monotonic() - started > settings["timeout_seconds"]:
                    raise TimeoutError("客户端媒体对照达到研究总超时")
                block = response.read1(settings["sample_bytes"] - count)
                if not block:
                    break
                count += len(block)
    except Exception as error:
        result["error"] = safe_message(error)
    result.update(bytes=count, seconds=round(time.monotonic() - started, 3))
    return result


def main() -> None:
    """按 --config 短时观察显式 --pid 的腾讯客户端，自动分离会话并保存脱敏参数与媒体吞吐报告。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    args = parser.parse_args()
    path = args.config.resolve()
    research, config = load_research(path)
    settings = research["native_observation"]
    process = psutil.Process(args.pid)
    if process.name() not in settings["process_names"] or not Path(process.exe()).resolve().is_relative_to(config["playback"]["native"]["dll_root"]):
        parser.error("--pid 不是专项配置安装目录中的目标腾讯客户端")
    mappings = process.memory_maps(grouped=True)
    constant_modules = {Path(value.path) for value in mappings
                        if Path(value.path).name in settings["signing"]["constant_modules"]}
    constant_images = {path.name: path.read_bytes() for path in constant_modules}
    for key in ("download_proxy", "signing"):
        profile = settings[key]
        paths = {Path(value.path) for value in mappings if Path(value.path).name.lower() == profile["module"].lower()}
        if len(paths) != 1 or hashlib.sha256(next(iter(paths)).read_bytes()).hexdigest() != profile["sha256"]:
            parser.error(f"目标进程的 {key} ABI 尚未核对，拒绝观察原生参数")
    config = deepcopy(config)
    config["network"]["timeout_seconds"] = research["transfer_probe"]["timeout_seconds"]
    transport = Transport(config)
    from local.playback.tasks import requests
    known_videos = {item.video_id for item in requests(config)}
    events, report = Queue(), {"requests": [], "native_arguments": [], "signing": [], "media_probes": [], "errors": []}
    last_signing = []
    def on_message(message, data):
        """将 Frida message 事件放入本进程内存队列；data 为未使用二进制载荷；无返回值且不直接打印原文。"""
        if message.get("type") == "send":
            events.put(message["payload"])
        elif message.get("type") == "error":
            events.put({"kind": "error", "message": safe_message(message.get("description", "观察脚本错误"))})
    session = frida.attach(args.pid)
    script = session.create_script(script_text(settings, research["native_logs"]["endpoints"]))
    try:
        script.on("message", on_message)
        script.load()
        end = time.monotonic() + settings["watch_seconds"]
        while time.monotonic() < end:
            try:
                event = events.get(timeout=config["runtime"]["cancel_poll_interval_seconds"])
            except Empty:
                continue
            if event["kind"] == "ready":
                print(f"观察已就绪：PID={args.pid}，模块={event['module']}；请触发一次目标播放查询。", flush=True)
            elif event["kind"] in ("api", "other"):
                summary = summarize_request(event, research["native_logs"]["query_fields"])
                params, _ = request_query(event)
                if params.get("cKey"):
                    ckey = params["cKey"][0]
                    summary["signer_matches"] = [{"ckey_offset": output.find(ckey), "output_length": len(output)}
                                                 for output in last_signing if ckey and ckey in output]
                report["requests"].append(summary)
                print(json.dumps(summary, ensure_ascii=False), flush=True)
            elif event["kind"] == "media" and len(report["media_probes"]) < settings["max_media_probes"]:
                summary = probe_media(transport, event["url"], research["transfer_probe"])
                report["media_probes"].append(summary)
                print(json.dumps(summary, ensure_ascii=False), flush=True)
            elif event["kind"] == "native":
                shapes, urls = native_arguments(event["values"], research["native_logs"]["query_fields"])
                summary = {"function": event["name"], "shapes": shapes, "url_count": len(urls),
                           "resources": [{"host": urlsplit(url).hostname, "extension": Path(urlsplit(url).path).suffix,
                                          "query_names": sorted(parse_qs(urlsplit(url).query))} for url in urls]}
                report["native_arguments"].append(summary)
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                for url in urls:
                    parsed = urlsplit(url)
                    if parsed.path.rsplit("/", 1)[-1] in research["native_logs"]["endpoints"]:
                        request = summarize_request({"url": url, "body": event["values"][-1]}, research["native_logs"]["query_fields"])
                        report["requests"].append(request)
                        print(json.dumps(request, ensure_ascii=False), flush=True)
                    elif (len(report["media_probes"]) < settings["max_media_probes"]
                          and any(parsed.hostname == domain or (parsed.hostname or "").endswith("." + domain) for domain in settings["media_domains"])
                          and Path(parsed.path).suffix in (".m3u8", ".ts", ".mp4")):
                        result = probe_media(transport, url, research["transfer_probe"])
                        report["media_probes"].append(result)
                        print(json.dumps(result, ensure_ascii=False), flush=True)
            elif event["kind"] == "signing":
                last_signing.append(event.get("output", ""))
                last_signing[:] = last_signing[-settings["max_events"]:]
                summary = signing_summary(event, settings["signing"], known_videos)
                for source, shape in zip(event["strings"], summary["strings"]):
                    if source["index"] in (0, 1, 2, 3) and source["value"]:
                        encoded = source["value"].encode("utf-8")
                        shape["embedded_constants"] = [{"module": name, "file_offset": content.find(encoded)}
                                                       for name, content in constant_images.items() if encoded in content]
                report["signing"].append(summary)
                print(json.dumps(summary, ensure_ascii=False), flush=True)
            elif event["kind"] == "error":
                report["errors"].append(event["message"])
                print(event["message"], flush=True)
    finally:
        try:
            report["calls"] = script.exports_sync.summary()
            print(f"观察调用计数：{report['calls']}", flush=True)
        except Exception as error:
            report["errors"].append(safe_message(error))
        finally:
            try:
                script.unload()
            finally:
                session.detach()
        target = resolve_path(research["report_dir"], path.parent) / f"native_observation_{args.pid}.json"
        atomic_json(target, report)
        print(f"已分离观察会话，脱敏报告：{target}", flush=True)


if __name__ == "__main__":
    main()
