"""依据单集验收报告，对已发布 URL 成品补验当前启用而尚未验证的解码设备，不重新下载或改动媒体。"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.batch import Batch
from local.configuration import load_config
from local.journal import Journal
from local.playback.transport import safe_message
from local.storage import atomic_json, fingerprint, output_lock


def main() -> int:
    """读取 --config/--report 的已完成单集，验证来源摘要并补验启用设备；--report-dir 保存证明；返回退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path, help="tests.verify_playback --full-download 生成的报告")
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()
    config = load_config(args.config, source="url")
    source = json.loads(args.report.read_text(encoding="utf-8"))
    completed = next((value for value in source["entries"] if value.get("status") == "completed"), None)
    if completed is None:
        parser.error("输入报告没有首次成功发布的单集")
    target = Path(completed["output"]).resolve()
    if not target.is_relative_to(config["output_path"]):
        parser.error("报告成品不属于主配置的输出目录")
    digest = fingerprint(target, config["copy_buffer_bytes"], force=True)
    if digest["sha256"] != completed["sha256"]:
        parser.error("当前成品摘要与单集报告不符")
    proof = completed["verification"]
    if proof.get("exit_code") != 0:
        parser.error("单集报告没有已通过的解码证明")
    results, code = [proof], 0
    with output_lock(config["output_path"], config["logging"]["lock_file"]):
        journal = Journal(config)
        try:
            for device, enabled in config["decode_verification"].items():
                if not enabled or device == proof["device"]:
                    continue
                trial = deepcopy(config)
                trial["decode_verification"] = {name: name == device for name in config["decode_verification"]}
                batch = Batch(trial, journal)
                work = config["work_dir"] / "url_output_verification" / uuid4().hex
                work.mkdir(parents=True, exist_ok=False)
                entry = journal.add({"video_id": source["task"]["video_id"], "title": source["task"]["title"]})
                journal.update(entry, work_dir=str(work))
                try:
                    current = batch.verify(target, work, source["selected"]["duration_seconds"], entry)
                    results.append(current)
                    journal.update(entry, status="completed", detail=f"{device.upper()} 完整解码通过", progress=100)
                except Exception as error:
                    code = 1
                    journal.update(entry, status="failed", detail=safe_message(error))
                    results.append({"device": device, "exit_code": 1, "error": safe_message(error)})
                    break
            if fingerprint(target, config["copy_buffer_bytes"], force=True)["sha256"] != digest["sha256"]:
                code = 1
                results.append({"exit_code": 1, "error": "设备补验期间成品内容发生变化"})
        finally:
            journal.close("failed" if code else "completed", {"failed": code})
    result = {"input_report": str(args.report.resolve()), "output": str(target), "sha256": digest["sha256"],
              "devices": results, "exit_code": code}
    args.report_dir.mkdir(parents=True, exist_ok=True)
    report = args.report_dir / f"verify_url_output_{uuid4().hex}.json"
    atomic_json(report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"成品设备补验报告：{report}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
