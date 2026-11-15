"""只读核对客户端下载数据库的完成/进行中记录结构，不读取任何视频或票据内容。"""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sqlite3
from xml.etree import ElementTree as ET


def main() -> int:
    """检查 --db 指定的 SQLite 元数据；仅输出记录名称、XML 标签及下载状态相关属性，成功返回 0。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="客户端 PPSDownLoad.db 路径，只读打开")
    parser.add_argument("--encoding", help="显式指定数据库 XML 的实际编码；不填时核对 XML 声明的编码")
    args = parser.parse_args()
    connection = sqlite3.connect(args.db.expanduser().resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT Name, Data FROM table_file").fetchall()
    finally:
        connection.close()
    summaries = []
    for name, blob in rows:
        result = {"record": name, "bytes": len(blob)}
        if not name.lower().endswith(".xml"):
            summaries.append(result)
            continue
        encoding = re.search(rb"encoding=['\"]([^'\"]+)['\"]", blob[:160], re.I)
        result["encoding"] = encoding[1].decode("ascii") if encoding else "utf-8"
        result["decoder"] = args.encoding or result["encoding"]
        try:
            root = ET.fromstring(blob.decode(result["decoder"]))
            items = list(root.iter())
            result["tags"] = dict(Counter(node.tag for node in items))
            result["attributes"] = sorted({key for node in items for key in node.attrib})
            candidates = [node for node in items if "SaveFileName" in node.attrib]
            result["existing_exact_paths"] = sum(
                (Path(node.get("SaveDir", "")) / node.get("SaveFileName", "")).is_file() for node in candidates)
            result["state_counts"] = dict(Counter(node.get("State", "") for node in candidates))
            result["examples"] = [
                {key: value for key, value in node.attrib.items()
                 if key in {"SaveDir", "SaveFileName", "FileSize"}
                 or re.search("status|state|progress|finish|complete", key, re.I)}
                for node in candidates[:2] + candidates[-2:]
            ]
        except (ValueError, ET.ParseError) as error:
            result["error"] = str(error)
        summaries.append(result)
    print(json.dumps(summaries, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
