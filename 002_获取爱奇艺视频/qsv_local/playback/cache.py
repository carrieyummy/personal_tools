"""只读研究播放缓存索引，报告不含账号、票据或媒体下载地址。"""

from datetime import datetime
import hashlib
from pathlib import Path

from ..storage import atomic_json
from .configuration import read_profile
from .metadata import linked_videos


def cache_snapshot(profile: dict, tvids: list[str]) -> dict:
    """只读查询 profile 的原生播放缓存索引中 tvids 对应的记录；返回有界结构，不发起网络媒体传输。"""
    import frida
    import psutil

    settings, root = profile["native"], profile["client_root"]
    candidates = []
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if (process.info["name"] != settings["process_name"] or not process.info["exe"]
                    or not Path(process.info["exe"]).resolve().is_relative_to(root)):
                continue
            paths = [Path(value.path).resolve() for value in process.memory_maps(grouped=True)
                     if Path(value.path).name.casefold() == settings["module"].casefold()]
            if len(paths) == 1:
                candidates.append((process.pid, paths[0]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if len(candidates) != 1:
        raise RuntimeError("需要唯一的原生播放缓存进程；请打开已登录客户端并播放一集正片")
    pid, module = candidates[0]
    if not module.is_relative_to(root) or hashlib.sha256(module.read_bytes()).hexdigest() != settings["sha256"]:
        raise RuntimeError("播放缓存模块未经当前 ABI 核验，停止查询")
    session, script = None, None
    try:
        session = frida.attach(pid)
        script = session.create_script(Path(__file__).with_suffix(".js").read_text(encoding="utf-8"))
        script.load()
        if Path(script.exports_sync.describe(settings["module"])).resolve() != module:
            raise RuntimeError("原生进程加载的模块路径发生变化")
        value = script.exports_sync.inspect(settings, tvids)
        if not value["databases"]:
            raise RuntimeError("未找到已打开的 PGF 播放缓存索引，不猜测数据库密钥或对象地址")
        for database in value["databases"]:
            for entry in database["paths"]:
                if not Path(entry["PGF_PATH"]).resolve().is_relative_to(profile["cache_root"]):
                    raise RuntimeError("原生索引中的 PGF 路径不属于配置的播放缓存目录")
        return value
    finally:
        try:
            if script is not None:
                script.unload()
        finally:
            if session is not None:
                session.detach()


def coverage(files: list[dict]) -> list[dict]:
    """汇总 files 的分段/块覆盖；返回仅基于索引的完整性判断，不代替媒体或摘要验证。"""
    groups = {}
    for row in files:
        key = (row["TVID"], row["VID"], row["FILE_TYPE"])
        group = groups.setdefault(key, {"tvid": key[0], "vid": key[1], "file_type": key[2],
                                        "segments": [], "totals": set(), "bytes": 0, "complete_segments": []})
        index, total = int(row["SEG_INDEX"]), int(row["SEG_TOTAL_COUNT"])
        if index in group["segments"] or index < 0 or total <= 0 or index >= total:
            raise ValueError("播放缓存索引的片段编号不唯一或超出总数")
        group["segments"].append(index)
        group["totals"].add(total)
        size, block_size = int(row["FILE_SIZE"]), int(row["FILE_BLOCK_SIZE"])
        cached_size, blocks = int(row["CACHED_BYTES"]), int(row["CACHED_BLOCKS"])
        if size <= 0 or block_size <= 0 or cached_size < 0 or blocks < 0:
            raise ValueError("播放缓存索引的文件/块大小无效")
        needed = (size + block_size - 1) // block_size
        group["bytes"] += cached_size
        if (cached_size == size and blocks == needed and row["FIRST_BLOCK"] is not None
                and int(row["FIRST_BLOCK"]) == 0 and int(row["LAST_BLOCK"]) == needed - 1):
            group["complete_segments"].append(index)
    result = []
    for group in groups.values():
        if len(group["totals"]) != 1:
            raise ValueError("同一缓存来源的片段总数不一致")
        total = group.pop("totals").pop()
        group["segments"].sort()
        group["complete_segments"].sort()
        group.update(total_segments=total, index_coverage_complete=group["complete_segments"] == list(range(total)))
        result.append(group)
    return result


def inspect_cache(path: Path) -> tuple[dict, Path]:
    """按研究配置 path 解析链接并生成只读缓存覆盖报告；返回报告及原子保存路径。"""
    profile = read_profile(path)
    if not profile["cache_root"].is_dir():
        raise ValueError("cache_root 必须是实际播放缓存目录")
    links = linked_videos(profile)
    snapshot = cache_snapshot(profile, [value["tvid"] for value in links])
    report = {"mode": "read_only_playback_cache", "links": links, "cache": snapshot,
              "coverage": [item for database in snapshot["databases"] for item in coverage(database["files"])],
              "highest_quality_verified": False, "media_verified": False,
              "download_tasks_created": False, "module_sha256": profile["native"]["sha256"]}
    name = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f") + ".json"
    output = profile["report_dir"] / name
    atomic_json(output, report)
    return report, output

