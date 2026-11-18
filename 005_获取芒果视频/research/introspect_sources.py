"""保存正式原生请求的脱敏结构与档位，核实选集分类、分页和最高档能力声明。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local.client import NativeClient, normalize_url
from local.configuration import read_config, read_yaml


def clean(value):
    """清理 API 树；value 为响应对象；返回移除地址、票据、账号标识和密钥值后的副本。"""
    if isinstance(value, dict):
        excluded = {"frame", "preview", "point", "adparams", "shadow", "shadowsources", "adsourceShadowsources", "authinfo", "drminfo", "config"}
        return {key: clean(child) for key, child in value.items() if key.lower() not in excluded and not any(term in key.lower() for term in ("url", "ticket", "uuid", "token", "mac", "phone", "avatar", "image", "seek"))}
    if isinstance(value, list):
        return [clean(child) for child in value]
    if isinstance(value, str) and ("http" in value or len(value) > 250):
        return "[omitted]"
    return value


def main() -> int:
    """解析主配置和清单路径并检查所有目标；无参数；返回运行退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--list", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    client = NativeClient(read_config(args.config))
    result = []
    for entry in read_yaml(args.list)["download_list"]:
        _, clip, video = normalize_url(entry)
        source, status = client.raw_source(clip, video)
        catalog = client.catalog_page(clip, 1)
        result.append({"clip_id": clip, "video_id": video, "status": status, "source": clean(source), "catalog": clean(catalog)})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in result:
        print(json.dumps({"clip": item["clip_id"], "status": item["status"], "source_fields": list(item["source"].get("data", {})), "catalog_fields": list(item["catalog"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
