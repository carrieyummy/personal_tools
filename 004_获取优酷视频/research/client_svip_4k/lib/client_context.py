"""只读提取客户端日志中的请求上下文；设备标识和完整 UA 仅在内存使用。"""

import json
import re
from typing import Any


def load_client_context(config: dict[str, Any], version: str) -> dict[str, Any]:
    """读取最后一条完整 UA；config 含日志位置和提取规则，version 为完整安装版本；返回内存上下文，缺失或版本不符抛 ValueError。"""
    options = config["client_context"]
    path = config["user_data"] / options["log_path"]
    matches = list(re.finditer(options["user_agent_pattern"], path.read_text(encoding="utf-8"), re.MULTILINE))
    if not matches:
        raise ValueError("客户端日志没有可用的完整 User-Agent")
    match = matches[-1]
    ua = match.group("user_agent")
    values = {}
    for name, pattern in options["field_patterns"].items():
        field = re.search(pattern, ua)
        if field is None:
            raise ValueError(f"客户端 User-Agent 缺少字段：{name}")
        values[name] = field.group(1)
    if values["build"] != version:
        raise ValueError("日志中的客户端版本与安装目录不一致，不能作为当前上下文")
    data = json.loads((config["user_data"] / options["state_path"]).read_text(encoding="utf-8"))
    for key in options["capability_path"]:
        data = data[key]
    bits = int(data)
    return {"user_agent": ua, "device": values["device"],
            "biz_params": {key: values[field] for key, field in options["biz_field_mapping"].items()},
            "summary": {"log_timestamp_local": match.group("timestamp"), "build": values["build"],
                        "zreal_header": int(values["zreal"]), "device_present": bool(values["device"]),
                        "cached_capability_bits": bits,
                        "cached_capabilities": {key: bool(bits & (1 << bit))
                                                for key, bit in options["capability_bits"].items()},
                        "note": "日志和保存的能力可能来自历史播放；不证明本次样本流或当前运行进程。"}}
