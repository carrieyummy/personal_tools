"""只读提取客户端播放参数附近的静态字符串，不读取用户文件或运行 DLL。"""

import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local.configuration import expand_path, read_yaml


def main() -> int:
    """按 CLI 专项配置读取播放器二进制；无参数；打印参数字符串并返回 0。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = read_yaml(args.config)
    root = expand_path(config["paths"]["client_install"], args.config.parent)
    for name in config["inspection"]["binaries"]:
        strings = [value.decode("ascii") for value in re.findall(rb"[\x20-\x7e]{4,200}", (root / name).read_bytes())]
        terms = [value for value in strings if re.search(r"(?:localDefinition|initDefinition|localVideo|localFile|videoIntroduction|definitionType|hdts|exdef|&ctk|sceen_|screen_hdr)", value)]
        print(json.dumps({"file": name, "terms": terms}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
