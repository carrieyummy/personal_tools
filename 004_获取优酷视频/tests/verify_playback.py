"""正式优酷流程的限量真实验收；默认只查目录和来源，显式开关才下载选定集。"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local.application import UrlBatch
from local.configuration import load_config
from local.errors import safe_message
from local.journal import Journal
from local.storage import atomic_json, output_lock
from local.tasks import discover, requests
from local.youku.source import resolve
from local.youku.inspector import LiveInspector, evaluate_player


def main() -> int:
    """解析配置、清单序号和集数上限；默认只取源，--full-download 执行限量真实下载并保存报告；返回验收退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,required=True)
    parser.add_argument("--list-index",type=int,required=True)
    parser.add_argument("--episodes",type=int,required=True)
    parser.add_argument("--full-download",action="store_true")
    args = parser.parse_args()
    if args.list_index < 0 or args.episodes <= 0:
        parser.error("清单序号须非负，集数须为正数")
    config = load_config(args.config)
    selected = requests(config)[args.list_index]
    selected["all_parts"] = True
    with output_lock(config["output_path"], config["logging"]["lock_file"]), output_lock(config["work_dir"] / "url", config["logging"]["lock_file"]):
        journal = Journal(config)
        print("progress",journal.url,flush=True)
        batch = UrlBatch(config,journal)
        status, summary = "failed", {}
        try:
            tasks, failed = discover(batch.client,[selected])
            if failed:
                print(json.dumps(failed,ensure_ascii=False),flush=True)
                return 1
            tasks = tasks[:args.episodes]
            if len(tasks) != args.episodes:
                raise ValueError("节目没有足够的已上线正片")
            print("tasks",json.dumps([{k:t[k] for k in ("video_id","title","index","release_date")} for t in tasks],ensure_ascii=False),flush=True)
            if args.full_download:
                summary = batch.run(tasks)
            else:
                for task in tasks:
                    source = resolve(batch.client,task)
                    print("source",json.dumps({"description":source.description,"identity":source.identity,"segments":len(source.segments),
                        "clear_segments":sum(s["private"].get("DRM_NOTENCRYPT")==1 for s in source.segments),
                        "discontinuities":sorted({s["discontinuity"] for s in source.segments})},ensure_ascii=False),flush=True)
                summary = {"resolved":len(tasks),"failed":0}
            status = "completed" if not summary.get("failed") else "failed"
            with LiveInspector(batch.client.profile,batch.client.pid) as inspector:
                current_vid=evaluate_player(batch.client.profile,inspector,"window.videoPlayer.context.config.vid")
            unchanged=current_vid==batch.client.initial_vid
            print("client_page_unchanged",unchanged,flush=True)
            if not unchanged:
                status="failed"
            print("summary",json.dumps(summary,ensure_ascii=False),flush=True)
            atomic_json(config["work_dir"] / "verification_latest.json",{"summary":summary,"full_download":args.full_download,
                "client_page_unchanged":unchanged,"tasks":[{k:t[k] for k in ("video_id","title","index")} for t in tasks],"entries":journal.run["entries"]},config["storage"])
            return 0 if status == "completed" else 1
        except Exception as error:
            import traceback
            print("error",safe_message(error),"frames",[(Path(f.filename).name,f.lineno,f.name) for f in traceback.extract_tb(error.__traceback__)],flush=True)
            return 1
        finally:
            print("report",journal.close(status,summary),flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
