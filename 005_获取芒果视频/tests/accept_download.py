"""显式实网验收：从现有清单选一集，执行完整下载/解码/发布及第二次指纹复用。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.application import run
from local.configuration import read_config, read_yaml


def main() -> int:
    """读取命令行主配置及清单条目索引；无参数；写独立验收配置，下载一集并复用，返回失败码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--list", type=Path, required=True)
    parser.add_argument("--item", type=int, required=True, help="清单中的零基索引")
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    loaded = read_config(args.config)
    config = read_yaml(args.config)
    for key in ("output_path", "work_dir", "ffmpeg_path", "native_profile", "default_list"):
        config[key] = str(loaded[key])
    config["auth"]["user_data"] = str(loaded["auth"]["user_data"])
    config["work_dir"] = str(loaded["work_dir"] / "acceptance")
    config["download"]["keep_intermediates"] = True
    args.report_dir.mkdir(parents=True, exist_ok=True)
    path = args.report_dir / "acceptance_config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    listing = read_yaml(args.list)
    item = listing["download_list"][args.item]
    selected = args.report_dir / "acceptance_list.yaml"
    selected.write_text(yaml.safe_dump({**listing, "download_list": [item]}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    options = argparse.Namespace(config=path, url=[str(selected)], all_parts=False, login_only=False, list_formats=False)
    codes = []
    for _ in range(2):
        code = run(options)
        codes.append(code)
        if code:
            break
    state = json.loads((loaded["output_path"] / config["logging"]["state_file"]).read_text(encoding="utf-8"))
    last = state["runs"][-1]
    reused = any(value["stage"] == "复用" for value in last["items"].values())
    report = {"codes": codes, "reused": reused, "output_path": config["output_path"], "last_run": last}
    (args.report_dir / "acceptance_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(codes != [0, 0] or not reused)


if __name__ == "__main__":
    raise SystemExit(main())
