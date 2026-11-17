"""使用当前配置中的真实目录验收暂停会话跨集下载及重复运行；不改用户配置，不切换客户端界面。"""

import argparse
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsv_local.configuration import resolve_path
from qsv_local.download_state import DownloadStatus
from qsv_local.playback.configuration import read_profile
from qsv_local.playback.listing import expand_episodes, task_options
from qsv_local.playback.media import media_config
from qsv_local.playback.metadata import linked_videos
from qsv_local.storage import read_json


def download_records(config: dict, main_path: Path) -> tuple:
    """按 config/main_path 只读获取客户端下载完成、进行中及命名记录；返回可比较快照，不读取视频内容。"""
    config = deepcopy(config)
    guard = config["download_guard"]
    guard["database_path"] = resolve_path(guard["database_path"], main_path.parent)
    status = DownloadStatus(config)
    status.refresh()
    return (status.completed, status.active, status.metadata)


def invoke_entry(root: Path, config: Path) -> dict:
    """使用当前 Python 运行 root 的正式入口和临时 config，实时转发输出；成功返回其批次报告，失败抛错。"""
    process = subprocess.Popen([sys.executable, "-X", "utf8", str(root / "download_iqiyi.py"), "--config", str(config)],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
    report = None
    try:
        for line in process.stdout:
            print(line, end="", flush=True)
            if line.startswith("批次报告："):
                report = Path(line.split("：", 1)[1].strip())
        code = process.wait()
        if code or report is None:
            raise RuntimeError(f"正式下载入口失败，退出码 {code}")
        return read_json(report)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()


def main() -> int:
    """解析真实配置、链接下标和目录位置；可选重复运行以验收复用；成功返回 0，失败返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="实际播放专项配置")
    parser.add_argument("--index", type=int, required=True, help="实际下载清单下标，从 0 开始")
    parser.add_argument("--episode-position", type=int, required=True, help="该专辑可用正片目录中的位置，从 0 开始")
    parser.add_argument("--repeat", action="store_true", help="成功后再次运行，要求复用相同成品")
    args = parser.parse_args()
    if args.index < 0 or args.episode_position < 0:
        parser.error("链接下标和目录位置必须为非负整数")
    try:
        root, profile = Path(__file__).resolve().parents[1], read_profile(args.config)
        link = linked_videos(profile, [args.index])[0]
        episodes = expand_episodes(profile, link, True)
        if args.episode_position >= len(episodes):
            raise ValueError("目录位置超过当前官方可用正片范围")
        video, options = episodes[args.episode_position], task_options(profile)[args.index]
        print(f"验收目标：{video['title']} 第 {video['episode']} 集；不打开该集客户端界面。", flush=True)
        raw = yaml.safe_load(args.config.read_text(encoding="utf-8-sig"))
        for key in ("main_config", "cache_root", "report_dir"):
            raw[key] = str(profile[key])
        config = media_config(profile)
        before = download_records(config, profile["main_config"])
        with tempfile.TemporaryDirectory(prefix="iqiyi_accept_") as directory:
            temporary = Path(directory)
            listing = temporary / "links.yaml"
            listing.write_text(yaml.safe_dump({"quality": options["quality"], "download_list": [
                {"url": video["play_url"], "all-parts": False}]}, allow_unicode=True), encoding="utf-8")
            raw["download_list"] = str(listing)
            path = temporary / "playback.yaml"
            path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            resolved = linked_videos(read_profile(path))[0]
            if resolved["tvid"] != video["tvid"]:
                raise RuntimeError("普通播放页链接解码与官方目录不一致")
            first = invoke_entry(root, path)
            if first["completed"] + first["reused"] != 1 or first["failed"]:
                raise RuntimeError("正式入口未完成唯一验收任务")
            if args.repeat:
                second = invoke_entry(root, path)
                if second["reused"] != 1 or second["completed"] or second["failed"]:
                    raise RuntimeError("第二次运行没有可靠复用同一成品")
        if download_records(config, profile["main_config"]) != before:
            raise RuntimeError("验收期间客户端下载列表发生变化")
        print("验收通过：正式入口、普通播放页链接及客户端下载列表保护均通过。", flush=True)
        return 0
    except Exception as error:
        print(f"链接模式验收失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
