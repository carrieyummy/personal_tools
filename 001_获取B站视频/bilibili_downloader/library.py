"""管理投稿记录入口、合集归档及本地和原站信息刷新。"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Iterator

from . import api
from . import auth
from . import metadata as metadata_store
from . import naming
from . import provenance
from . import identity
from .merged_records import MergedVideoInfo
from .config import Settings
from .errors import ToolError
from .metadata import VideoInfo
from .models import Quality


_locks_guard = Lock()
_video_locks: dict[tuple[str, str], Any] = {}


@contextmanager
def video_transaction(settings: Settings, bvid: str, title: str | None = None,
                      total_parts: int | None = None, *, prepare: bool = False,
                      shared_folder: Path | None = None) -> Iterator[VideoInfo]:
    """按 settings 根目录和 bvid 加锁并重新读取记录；title/total_parts 供新建，prepare 启用旧布局整理。

    shared_folder 在 prepare 时指定合集目录；产生最新 VideoInfo，调用方显式 save；锁内不执行网络或 FFmpeg。
    """
    key = (str(settings.output_dir.resolve()).casefold(), bvid)
    with _locks_guard:
        lock = _video_locks.setdefault(key, RLock())
    with lock:
        record = get_video_info(settings, bvid, title, total_parts, shared_folder=shared_folder) if prepare else metadata_store.open_video(settings.output_dir, bvid, title, total_parts)
        yield record


def complete_video(settings: Settings, bvid: str, video_id: str, quality: Quality, path: Path, executable: str,
                   *, source_cid: int, variant: str = '', part: int | None = None) -> None:
    """按 settings 保存 bvid/video_id/quality 的 source_cid 版本并用 executable 探测 path；variant/part 定位多版本，返回 None。"""
    with video_transaction(settings, bvid) as record:
        item = record.entry(video_id, quality=quality, part=part, variant=variant)
        if item.get('source_cid') != source_cid:
            raise ToolError('源视频版本在保存期间发生变化，请重新运行')
        record.record_file(item, path)
        item['errors'] = []
        record.save()
    record.enrich_file(item, executable)
    attributes = {key: item[key] for key in ('duration_seconds', 'width', 'height', 'fps', 'video_codec', 'audio_codec', 'errors')}
    with video_transaction(settings, bvid) as current:
        latest = current.entry(video_id, quality=quality, part=part, variant=variant)
        if latest.get('source_cid') != source_cid:
            raise ToolError('源视频版本在媒体探测期间发生变化，请重新运行')
        latest.update(attributes)
        current.save()


def snapshot_video(settings: Settings, bvid: str) -> VideoInfo:
    """按 settings/bvid 加锁读取当前布局的独立记录快照；不迁移目录，返回可在锁外查询的 VideoInfo。"""
    with video_transaction(settings, bvid) as record:
        return record


def cache_source_fingerprints(settings: Settings, record: VideoInfo,
                              sources: list[tuple[Path, dict[str, Any]]]) -> None:
    """按 settings 加锁，将 record 的 sources 指纹合并到最新记录；来源版本或文件变化时报错，不覆盖并发更新，返回 None。"""
    with video_transaction(replace(settings, output_dir=record.output_dir), record.data['source_id']) as current:
        entries = {item['id']: item for item in current.data['files']}
        expected = {page['part']: page['cid'] for page in current.data.get('current_parts', [])}
        changed = False
        for path, item in sources:
            latest = entries.get(item['id'])
            if (latest is None or latest.get('status') != 'completed'
                    or latest.get('source_cid') != item.get('source_cid') or latest['formats'] != item['formats']
                    or latest.get('episode_id') != item.get('episode_id')
                    or current.resolve_file(latest) != path
                    or (expected and expected.get(item['part']) != item.get('source_cid'))):
                raise ToolError(f"第 {item['part']} P 来源在合并期间变化，请重新运行")
            if current.data['source_type'] == 'bangumi':
                page = next((page for page in current.data.get('current_parts', []) if page['part'] == item['part']), None)
                if page is None or page['episode_id'] != item.get('episode_id'):
                    raise ToolError(f"第 {item['part']} P 剧集身份在合并期间变化，请重新运行")
            fingerprint = item['content_fingerprint']
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != (fingerprint['size_bytes'], fingerprint['mtime_ns']):
                raise ToolError(f"第 {item['part']} P 文件在合并期间变化，请重新运行")
            if latest.get('content_fingerprint') != fingerprint:
                latest['content_fingerprint'] = dict(fingerprint)
                changed = True
        if changed:
            current.save()


def adopt_existing_content(record: VideoInfo) -> tuple[int, int, list[str]]:
    """经用户明确认可后为 record 的现有媒体建立基线；使用已查询的 current_parts 补缺失 CID，不下载或拼接。

    仅补齐缺失的来源版本及合并快照，不将已知旧版本标为当前版本；返回源文件数、合并成品数和未能补齐的原因。
    无法追认建立基线之前的远端换片，调用方须确保没有并发下载或迁移。
    """
    expected = {page['part']: page['cid'] for page in record.data.get('current_parts', [])}
    sources, merged_count, errors = {}, 0, []
    for item in record.data['files']:
        if item.get('kind') != 'part' or item.get('status') != 'completed':
            continue
        try:
            path = record.resolve_file(item)
            fingerprint = provenance.file_fingerprint(path, item.get('content_fingerprint'))
            if item.get('source_cid') is None:
                if item['part'] not in expected:
                    raise ToolError('当前原站列表没有此分 P 的 CID，保留原信息')
                item['source_cid'] = expected[item['part']]
            if expected.get(item['part']) != item['source_cid']:
                raise ToolError('已知本地 CID 与当前原站不同，保留旧版本信息')
            item['content_fingerprint'] = fingerprint
            sources.setdefault((item['part'], item.get('quality_id')), []).append((path, item))
        except (OSError, ToolError) as exc:
            errors.append(f"{item['id']}：{exc}")
    record.save()
    for index_path in record.output_dir.rglob('video_info.json'):
        if not index_path.parent.name.endswith('_合并'):
            continue
        index = MergedVideoInfo(record.output_dir, index_path.parent)
        changed = False
        for item in index.data['files']:
            if item['source_id'] != record.data['source_id'] or item.get('source_snapshot') is not None:
                continue
            try:
                parts = item.get('source_parts')
                if not isinstance(parts, list) or len(parts) < 2 or any(type(part) is not int or part < 1 for part in parts):
                    raise ToolError('旧合并记录缺少有效的来源顺序，不能推断其内容')
                path = index.resolve_file(item)
                if not path.is_file() or not path.stat().st_size or path.stat().st_size != item.get('size_bytes'):
                    raise ToolError('旧合并成品缺失、为空或大小与记录不同')
                if any((part, item.get('quality_id')) not in sources for part in parts):
                    raise ToolError('部分源文件缺失或版本无法确认，保留旧合并记录')
                if any(len(sources[part, item.get('quality_id')]) != 1 for part in parts):
                    raise ToolError('源文件有多个编码版本，无法推断旧成品来源，保留旧记录')
                ordered = [sources[part, item.get('quality_id')][0] for part in parts]
                item['source_snapshot'] = provenance.source_snapshot(ordered)
                changed = True
                merged_count += 1
            except (OSError, ToolError) as exc:
                errors.append(f"{item['id']}：{exc}")
        if changed:
            index.save()
    return sum(len(values) for values in sources.values()), merged_count, errors


def get_video_info(settings: Settings, bvid: str, title: str | None = None, total_parts: int | None = None, *, shared_folder: Path | None = None) -> VideoInfo:
    """按 settings 聚合 bvid 的目录索引并迁移旧布局；title/total_parts 为投稿信息，shared_folder 为可选合集目录，返回 VideoInfo。"""
    if title is None and not metadata_store.find_video_folders(settings.output_dir, bvid):
        for path in settings.output_dir.rglob("download.json"):
            try:
                record = metadata_store.read_json(path)
            except ToolError:
                if bvid in str(path):
                    raise
                continue
            if re.fullmatch(re.escape(bvid) + r"_p\d+", str(record.get("id", ""))):
                title = record.get("video_title") or re.split(r"\s+p\d+\s+", record["title"], maxsplit=1, flags=re.I)[0]
                break
    record = metadata_store.open_video(settings.output_dir, bvid, naming.safe_name(title, settings.max_title_length) if title else None, total_parts, shared_folder=shared_folder)
    metadata_store.migrate_legacy(record)
    record.flatten_parts()
    record.organize_qualities(mix_quality=settings.mix_quality)
    if total_parts is not None:
        record.data["total_parts"] = total_parts
    if title:
        record.data["title"] = title
    return record


def prepare_collection(settings: Settings, metadata: dict[str, Any]) -> Path | None:
    """根据原站 metadata 定位合集并整理已下载的成员；settings 控制目录，非合集返回 None，否则返回合集 Path。"""
    season = metadata.get("ugc_season")
    if season is None:
        return None
    if not isinstance(season, dict) or type(season.get("id")) is not int or season["id"] < 1 or not isinstance(season.get("title"), str) or not season["title"]:
        raise ToolError("原站合集信息无效，无法确定保存目录")
    season_id = season["id"]
    matches = set()
    for path in settings.output_dir.glob(f"*_合集{season_id}*") if settings.output_dir.is_dir() else []:
        match = re.fullmatch(rf'(.+_合集{season_id})(?:_qn[1-9]\d*_.+|_旧版_未确认清晰度)?(?:_分P|_合并)?', path.name)
        if match:
            if path.is_symlink() or path.is_junction() or not path.is_dir():
                raise ToolError(f"合集目录无效：{path}")
            matches.add(path.with_name(match[1]))
    if len(matches) > 1:
        raise ToolError(f"合集 {season_id} 存在多个或无效目录：{matches}")
    folder = next(iter(matches)) if matches else settings.output_dir / f"{naming.safe_name(season['title'], settings.max_title_length)}_合集{season_id}"
    sections = season.get("sections")
    if not isinstance(sections, list):
        raise ToolError("原站合集缺少成员列表，无法整理已有视频")
    members = {metadata["bvid"]}
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("episodes"), list):
            raise ToolError("原站合集成员列表无效")
        for episode in section["episodes"]:
            if not isinstance(episode, dict) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", str(episode.get("bvid", ""))):
                raise ToolError("原站合集成员缺少有效 BV 号")
            members.add(episode["bvid"])
    for bvid in sorted(members):
        if metadata_store.find_video_folders(settings.output_dir, bvid):
            with video_transaction(settings, bvid, prepare=True) as member:
                metadata_store.move_to_collection(member, folder)
    return folder


def update_source_info(record: VideoInfo, metadata: dict[str, Any]) -> None:
    """将原站 metadata 的身份、标题及当前分P列表更新到 record；番剧保留季和剧集映射，不改写已下载版本、不写盘。"""
    if identity.source_id(metadata) != record.data['source_id']:
        raise ToolError("原站返回的来源标识与本地记录不一致")
    owner = metadata.get("owner") or {}
    record.data.update(title=metadata["title"], uploader={"id": owner.get("mid"), "name": owner.get("name")} if owner else None,
                       total_parts=len(metadata["pages"]) if metadata.get("pages") else metadata.get("videos", record.data["total_parts"]), errors=[])
    if metadata.get('pages'):
        pages = metadata['pages']
        if any(type(page.get('page')) is not int or page['page'] < 1
               or type(page.get('cid')) is not int or page['cid'] < 1 for page in pages):
            raise ToolError('原站分 P 编号或 CID 无效')
        if sorted(page['page'] for page in pages) != list(range(1, len(pages) + 1)):
            raise ToolError('原站分 P 列表不连续或重复')
        if record.data['source_type'] == 'bangumi':
            record.data.update(season_id=metadata['season_id'], source_url=metadata['source_url'])
            record.data['current_parts'] = [{**page, 'part': page['page'], 'title': page['part']} for page in pages]
        else:
            record.data['current_parts'] = [{'part': page['page'], 'cid': page['cid']} for page in pages]
    if not owner and record.data['source_type'] == 'video':
        record.data["errors"].append("原站未提供 UP 主信息")
    if record.data["total_parts"] is None:
        record.data["errors"].append("原站未提供总分 P 数")


def refresh_metadata(settings: Settings, args: argparse.Namespace, executable: str) -> int:
    """按 settings 迁移并刷新源投稿和独立合并索引；args 控制源站认证，executable 探测媒体，返回 0/2。"""
    video_ids, failures = metadata_store.discover_videos(settings.output_dir)
    records = []
    for bvid in video_ids:
        try:
            record = get_video_info(settings, bvid)
            record.refresh_local(executable)
            records.append(record)
        except (OSError, ToolError, KeyError) as exc:
            failures.append(f"{bvid}：{exc}")
    if records:
        try:
            with auth.authenticated_session(settings, args.cookies.expanduser().resolve() if args.cookies else None) as context:
                auth.ensure_login(context, settings)
                for record in records:
                    try:
                        if record.data['source_type'] == 'bangumi':
                            targets = api.resolve_targets(context, record.data['source_url'], settings, False)
                            metadata = targets[0].metadata
                        else:
                            metadata = api.api_json(context, "/x/web-interface/view", settings, {"bvid": record.data["source_id"]})["data"]
                        update_source_info(record, metadata)
                        record.flatten_parts()
                    except Exception as exc:
                        record.data["errors"] = [f"原站信息获取失败：{exc}"]
                    record.save()
        except Exception as exc:
            for record in records:
                record.data["errors"] = [f"原站认证或信息保存失败：{exc}"]
                record.save()
    for record in records:
        failures.extend(f"{record.data['source_id']}：{error}" for error in record.data["errors"])
        for item in record.data["files"]:
            failures.extend(f"{item['id']}：{error}" for error in item["errors"])
        print(f"视频信息已更新：{record.path}")
    refreshed = set()
    for path in list(settings.output_dir.rglob('video_info.json')):
        if not path.parent.name.endswith('_合并'):
            continue
        if not path.is_file() or path in refreshed:
            continue
        try:
            from .layout import organize_independent_merges

            index = MergedVideoInfo(settings.output_dir, path.parent)
            for destination in organize_independent_merges(index, settings.mix_quality):
                if destination in refreshed:
                    continue
                current = MergedVideoInfo(settings.output_dir, destination.parent)
                current.refresh_local(executable)
                refreshed.add(destination)
                for item in current.data['files']:
                    failures.extend(f"{item['id']} 合并信息：{error}" for error in item['errors'])
                print(f'合并视频信息已更新：{destination}')
        except (OSError, ToolError) as exc:
            failures.append(f'{path} 合并信息：{exc}')
    for failure in failures:
        print(failure, file=sys.stderr)
    return 2 if failures else 0
