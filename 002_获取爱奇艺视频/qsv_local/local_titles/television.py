"""本地电视剧和既有剧集记录的命名映射。"""

from pathlib import Path

from ..naming import output_name


def television_target(naming: dict, metadata: tuple, source: Path, output: Path) -> Path:
    """按 naming 和客户端 metadata 将 source 映射到 output 下的剧集目录，返回绝对目标路径。"""
    folder, name = output_name(naming, metadata, source.stem)
    return (output / folder / (name + ".mp4")).resolve()
