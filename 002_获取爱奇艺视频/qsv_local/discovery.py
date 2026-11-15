"""发现批量任务并建立保留输入目录层级的输出映射。"""

from dataclasses import dataclass
from pathlib import Path

from .download_state import DownloadStatus


@dataclass(frozen=True)
class Task:
    """不可变任务；source 为绝对 QSV 路径，relative 相对输入根目录，target 为首次输出路径。"""

    source: Path
    relative: Path
    target: Path


def discover(config: dict, *, pending: list | None = None) -> list[Task]:
    """按 config 递归发现已明确完成的 QSV；pending 可收集未完成/状态不明条目；
    大小写不敏感去重排序，返回任务列表，不读取任何视频内容或写文件。
    """
    root, output = config["input_root"], config["output_path"]
    status = DownloadStatus(config)
    status.refresh()
    sources, deferred = {}, {}
    for directory in config["input_path"] or [root]:
        for source in directory.rglob("*"):
            if source.suffix.lower() != ".qsv":
                if any(source.name.lower().endswith(".qsv" + suffix.lower())
                       for suffix in config["download_guard"]["temporary_suffixes"]):
                    deferred[str(source).casefold()] = {
                        "source": source.relative_to(root).as_posix(), "reason": "下载临时文件，禁止读取内容",
                    }
                continue
            source = source.resolve()
            if not source.is_relative_to(root):
                raise ValueError(f"QSV 链接指向 input_root 外部：{source}")
            reason = status.reason(source, refresh=False)
            if reason:
                deferred[str(source).casefold()] = {"source": source.relative_to(root).as_posix(), "reason": reason}
                continue
            if not source.is_file():
                continue
            sources[str(source).casefold()] = source
    if pending is not None:
        pending.extend(sorted(deferred.values(), key=lambda item: item["source"].casefold()))
    tasks = []
    targets = set()
    for source in sorted(sources.values(), key=lambda item: item.relative_to(root).as_posix().casefold()):
        relative = source.relative_to(root)
        target = (output / relative.with_suffix(".mp4")).resolve()
        if not target.is_relative_to(output):
            raise ValueError(f"输出链接超出 output_path：{target}")
        identity = str(target).casefold()
        if identity in targets:
            raise ValueError(f"多个输入映射到同一输出：{target}")
        targets.add(identity)
        tasks.append(Task(source, relative, target))
    return tasks
