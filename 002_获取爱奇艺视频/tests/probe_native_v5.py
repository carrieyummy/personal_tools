"""用项目当前 YAML 配置验证本机 v5 授权；复用正式实现，不输出票据或密钥。"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.configuration import read_config
from qsv_local.discovery import discover
from qsv_local.download_state import DownloadStatus
from qsv_local.container import QsvFile
from qsv_local.native import NativeV5


def main() -> int:
    """解析 --config 指定的项目 YAML，逐项验证选中 QSV 的授权；全部成功返回 0，有失败返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="与 decode_qsv.py 相同的项目 YAML 配置")
    args = parser.parse_args()
    try:
        config = read_config(args.config)
        results = []
        status = DownloadStatus(config)
        for task in discover(config):
            try:
                reason = status.reason(task.source)
                if reason:
                    results.append({"source": task.relative.as_posix(), "status": "skipped", "reason": reason})
                    continue
                source = QsvFile(task.source)
                with NativeV5(config["sdk"], source.ticket()):
                    results.append({"source": task.relative.as_posix(), "status": "completed",
                                    "license_version": str(source.info["license_version"]),
                                    "certificate_status": 0, "license_status": 0})
            except Exception as error:
                results.append({"source": task.relative.as_posix(), "status": "failed", "error": str(error)})
    except Exception as error:
        print(f"授权验证失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(results, ensure_ascii=False), flush=True)
    return 1 if any(item["status"] == "failed" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
