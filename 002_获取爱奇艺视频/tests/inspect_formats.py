"""只检查已完成下载的 QSV 格式分布，复用下载保护，不读取进行中或状态不明的视频。"""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.configuration import read_config
from qsv_local.container import QsvFile
from qsv_local.discovery import discover
from qsv_local.download_state import DownloadStatus


def main() -> int:
    """从 --config 发现明确完成的文件并按容器/授权版本统计；输出组别及无法识别项，存在识别错误返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="实际项目 YAML 配置，保留其中输入范围及下载保护")
    args = parser.parse_args()
    config = read_config(args.config)
    pending, errors, groups = [], [], Counter()
    tasks = discover(config, pending=pending)
    status = DownloadStatus(config)
    for task in tasks:
        reason = status.reason(task.source)
        if reason:
            pending.append({"source": task.relative.as_posix(), "reason": reason})
            continue
        try:
            source = QsvFile(task.source)
            groups[(source.container_version, str(source.info["license_version"]))] += 1
        except Exception as error:
            errors.append({"source": task.relative.as_posix(), "error": str(error)})
    print(json.dumps({"formats": [{"container_version": key[0], "license_version": key[1], "files": count}
                                  for key, count in sorted(groups.items())],
                      "pending_downloads": pending, "errors": errors}, ensure_ascii=True), flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
