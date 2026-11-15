"""按实际配置仅验收明确选中的样本，不改写用户配置或扩大测试媒体范围。"""

import argparse
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import decode_qsv
from qsv_local.configuration import read_config
from qsv_local.discovery import discover


def main() -> int:
    """用 --config 的正式入口验收可重复传入的 --source 相对路径；路径须在实际发现结果中，返回正式入口退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="实际项目 YAML 配置")
    parser.add_argument("--source", action="append", required=True, help="相对 input_root 的 QSV 路径，可重复传入")
    args = parser.parse_args()
    tasks = discover(read_config(args.config))
    available = {task.relative.as_posix().casefold(): task for task in tasks}
    selected = []
    for name in dict.fromkeys(Path(value).as_posix().casefold() for value in args.source):
        if name not in available:
            parser.error(f"样本不在配置指定的输入范围内：{name}")
        selected.append(available[name])
    with (patch("qsv_local.batch.discover", return_value=selected),
          patch.object(sys, "argv", ["decode_qsv.py", "--config", str(args.config)])):
        return decode_qsv.main()


if __name__ == "__main__":
    raise SystemExit(main())
