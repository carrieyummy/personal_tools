"""验证实际样本的分块读取一致性，以及错误文件头、截断和缺片的拒绝行为。"""

import argparse
from pathlib import Path
import tempfile

from local.configuration import load_config
from local.hls import discover_all, read_segments
from local.native import CacheLibrary, CacheReader, HEADER_SIZE


def verify(config: dict) -> None:
    """使用 config 对应的真实首片验证分块/整片一致性，并用临时目录验证清单错误；成功无返回值。"""
    tasks, _ = discover_all(config)
    if not tasks:
        raise ValueError("当前筛选条件没有已完成的真实下载样本")
    episode = tasks[0]
    segments, _ = read_segments(episode["folder"])
    raw = segments[0]["path"].read_bytes()
    library = CacheLibrary(config["native"])
    with CacheReader(library, raw[:HEADER_SIZE], episode["folder"]) as reader:
        whole = reader.decrypt(raw[HEADER_SIZE:], 0)
    with CacheReader(library, raw[:HEADER_SIZE], episode["folder"]) as reader:
        chunks = []
        # 刻意跨越非 TS 包边界，检验 native 接口的分片内字节偏移语义。
        step = min(config["copy_buffer_bytes"], max(1, len(whole) // 3))
        for offset in range(0, len(whole), step):
            chunks.append(reader.decrypt(raw[HEADER_SIZE + offset:HEADER_SIZE + offset + step], offset))
    assert whole == b"".join(chunks), "分块和整片还原结果不一致"
    assert all(value == 0x47 for value in whole[::188]), "整片 TS 失同步"
    print("PASS: 整片与跨边界分块还原一致", flush=True)
    for header in (raw[:HEADER_SIZE - 1], b"BAD!" + raw[4:HEADER_SIZE]):
        try:
            with CacheReader(library, header, episode["folder"]):
                raise AssertionError("错误文件头未被拒绝")
        except ValueError:
            pass
    print("PASS: 截断和错误文件头被拒绝", flush=True)
    with tempfile.TemporaryDirectory(prefix="tencent_cache_") as directory:
        folder = Path(directory)
        (folder / ".m3u8").write_text("#EXTM3U\n#EXTINF:1,\nsegment.ts?index=0&brs=0&bre=187\n#EXT-X-ENDLIST\n", encoding="utf-8")
        for payload in (None, b"HVN\0"):
            if payload is not None:
                (folder / "0.ts").write_bytes(payload)
            try:
                read_segments(folder)
                raise AssertionError("缺片或截断分片未被拒绝")
            except ValueError:
                pass
    print("PASS: 清单缺片和长度不符被拒绝", flush=True)


def main() -> None:
    """解析必填 --config 参数并运行验收；失败通过异常返回非零退出码，无返回值。"""
    parser = argparse.ArgumentParser(description="验证 HVN 本机适配和下载完整性检查")
    parser.add_argument("--config", type=Path, required=True, help="实际主 YAML 配置")
    args = parser.parse_args()
    verify(load_config(args.config, source="download"))


if __name__ == "__main__":
    main()
