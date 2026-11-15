"""连续解析同一清单项目并比较稳定身份；报告只含已脱敏元数据和资源摘要。"""

import argparse
from pathlib import Path
import sys
from urllib.parse import urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.configuration import load_config, resolve_path
from local.playback.audio import AudioSelector
from local.playback.client import NativeClient
from local.playback.source import resolve
from local.playback.tasks import requests
from local.storage import atomic_json


def main() -> None:
    """读取必填 --config，连续解析研究目标两次；输出来源差异数量并写无签名诊断报告，无返回值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    path = args.config.resolve()
    research = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    config = load_config(resolve_path(research["config"], path.parent), source="url")
    client, report = NativeClient(config), []
    for item in requests(config):
        if research["title"] and item.note != research["title"]:
            continue
        task = {"series_id": item.series_id, "video_id": item.video_id, "quality": item.quality}
        values = [resolve(client, task, AudioSelector(config["audio"])) for _ in range(2)]
        resources = [[resource for manifest in value.manifests for resource in manifest.resources] for value in values]
        changes = [{"index": index, "before": before.identity(), "after": after.identity(),
                    "before_basename": urlsplit(before.url).path.rsplit("/", 1)[-1],
                    "after_basename": urlsplit(after.url).path.rsplit("/", 1)[-1]}
                   for index, (before, after) in enumerate(zip(*resources)) if before.identity() != after.identity()]
        report.append({"note": item.note, "identities": [value.identity for value in values],
            "descriptions": [value.description for value in values], "resource_counts": list(map(len, resources)),
            "normalized_counts": [sum(resource.stable_path is not None for resource in group) for group in resources],
            "changes": changes})
        print(f"{item.note}：身份一致={values[0].identity == values[1].identity}；分片变化={len(changes)}", flush=True)
    output = resolve_path(research["report_dir"], path.parent) / "source_identity.json"
    atomic_json(output, {"items": report})
    print(f"脱敏身份诊断报告：{output}")


if __name__ == "__main__":
    main()
