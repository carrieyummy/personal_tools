"""读取腾讯官方页面当前引用的公开播放器运行时，保存协议字段上下文及内容指纹；不读取账号。"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.configuration import load_config, resolve_path
from research.settings import sample_requests as requests
from local.playback.transport import Transport
from local.storage import atomic_json


def main() -> None:
    """从 --config 读取研究目标、公开源过滤与报告目录；保存无账号信息的 JS 参考和上下文报告。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    path = args.config.resolve()
    research = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    config = load_config(resolve_path(research["config"], path.parent), source="url")
    transport = Transport(config)
    settings = research["runtime"]
    target = resolve_path(research["report_dir"], path.parent)
    target.mkdir(parents=True, exist_ok=True)
    report, seen = [], set()
    for item in requests(config):
        url = config["playback"]["catalog"]["page_template"].format(series_id=item.series_id, video_id=item.video_id)
        page = transport.text(url)
        for source in re.findall(r'<script[^>]+src="([^"]+)"', page):
            address = urljoin(url, source)
            if address in seen or urlsplit(address).hostname not in settings["allowed_hosts"]:
                continue
            if not any(re.search(pattern, source) for pattern in settings["source_patterns"]):
                continue
            seen.add(address)
            text = transport.text(address)
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            filename = f"public_runtime_{sha}.js"
            (target / filename).write_text(text, encoding="utf-8")
            item_report = {"public_source": address, "file": filename, "sha256": sha, "markers": {}}
            for marker in settings["markers"]:
                matches = list(re.finditer(re.escape(marker), text))
                width = settings["context_chars"]
                item_report["markers"][marker] = {"count": len(matches),
                    "contexts": [text[max(0, match.start() - width):match.end() + width]
                                 for match in matches[:settings["max_contexts"]]]}
            report.append(item_report)
    output = target / "public_runtime_inspection.json"
    atomic_json(output, {"sources": report})
    print(f"公开运行时数量：{len(report)}；脱敏研究报告：{output}")


if __name__ == "__main__":
    main()
