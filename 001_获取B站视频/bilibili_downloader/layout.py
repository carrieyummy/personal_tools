"""按清晰度和用途迁移投稿、合集和独立合并索引，协调跨记录事务。"""

from copy import deepcopy
from pathlib import Path
import re

from . import metadata, naming
from .errors import ToolError, check_cancel
from .merged_records import MergedVideoInfo
from .models import LEGACY_QUALITY, Quality, entry_quality, quality_entry_id
from .storage import apply_changes, validate_path


def folder_quality(base: Path, folder: Path) -> tuple[Quality, str] | None:
    """解析 base 的同级或旧嵌套 folder，返回明确画质和用途；旧目录用途为空，无法识别返回 None。"""
    suffix = folder.name.removeprefix(base.name + '_') if folder.parent == base.parent else folder.name
    if folder == base.with_name(base.name + '_分P'):
        return LEGACY_QUALITY, 'part'
    if folder == base.with_name(base.name + '_合并'):
        return LEGACY_QUALITY, 'merged'
    role = ''
    for ending, value in (('_分P', 'part'), ('_合并', 'merged')):
        if suffix.endswith(ending):
            suffix, role = suffix[:-len(ending)], value
            break
    match = re.fullmatch(r'qn([1-9]\d*)_(.+)', suffix)
    if match:
        return Quality(int(match[1]), match[2]), role
    return (LEGACY_QUALITY, role) if suffix == LEGACY_QUALITY.directory else None



def organize_submission(info: metadata.VideoInfo, target_base: Path | None = None, *, shared: bool = False,
                        mix_quality: bool | None = None, persist_source: bool = True) -> None:
    """整理 info 到 target_base；shared 改合集名，mix_quality 指定模式，persist_source=False 仅迁移成品，失败回滚，无返回值。"""
    original, old_path = deepcopy(info.data), info.path
    old_indexes, old_quality_folders = set(info._index_paths), dict(info._quality_folders)
    if old_path.exists():
        old_indexes.add(old_path)
        info._index_paths.add(old_path)
    base = info.base_directory
    target = (target_base if target_base is not None else base).resolve()
    validate_path(target, info.output_dir)
    was_mixed = info.mix_quality
    mixed = was_mixed if mix_quality is None else mix_quality
    moves: dict[Path, Path] = {}
    indexes: dict[Path, MergedVideoInfo] = {}
    obsolete: dict[Path, MergedVideoInfo] = {}
    old_folders = {base} if base.is_dir() else set()
    old_folders.update(folder for folder in base.parent.glob(base.name + '_*') if folder_quality(base, folder))
    if base.is_dir():
        old_folders.update(folder for folder in base.iterdir() if folder_quality(base, folder))
    if not persist_source:
        old_folders = {folder for folder in old_folders if folder_quality(base, folder)
                       and folder_quality(base, folder)[1] == 'merged'}
    known: set[Path] = set()
    stream_qualities: dict[str, set[Quality]] = {}
    registry = {entry_quality(item).code: entry_quality(item) for item in info.data['files']}

    def suffix_for(quality: Quality) -> str:
        """为 quality 返回稳定的实际画质标签；混合模式不沿用无法表示实际编号的旧目录别名。"""
        suffix = info.quality_suffix(quality)
        return quality.directory if mixed and quality.code is not None and not suffix.startswith(f'qn{quality.code}_') else suffix

    def destination_folder(quality: Quality, role: str = 'part') -> Path:
        """按目标布局为 quality 和 part/merged 用途返回绝对目录路径。"""
        return naming.role_folder(target, suffix_for(quality), mixed, role)

    def destination_media(item: dict, source: Path, quality: Quality, role: str, source_mixed: bool) -> Path:
        """按 item 的基础文件名将 source 映射到目标 quality/role；source_mixed 指明旧名称带画质后缀，返回路径并更新 item。"""
        stem = item.get('base_stem')
        if not stem:
            if source_mixed:
                raise ToolError(f'混合模式记录缺少基础文件名，不能推断恢复名称：{source}')
            stem = source.stem
        if not isinstance(stem, str) or Path(stem).name != stem or Path(stem).drive or stem in ('.', '..'):
            raise ToolError(f'基础视频文件名无效：{stem}')
        item.update(base_stem=stem, stem=naming.quality_stem(stem, suffix_for(quality), mixed), mix_quality=mixed)
        return destination_folder(quality, role) / (item['stem'] + source.suffix)

    def add_media(source: Path, destination: Path) -> None:
        """登记 source 和同前缀字幕到 destination 的移动及改名；缺失媒体保留记录，无返回值。"""
        known.add(source)
        if source != destination and source.exists():
            moves[source] = destination
        for sidecar in source.parent.glob(source.stem + '.*'):
            if sidecar.suffix.lower() in ('.srt', '.json', '.ass', '.vtt'):
                known.add(sidecar)
                new = destination.parent / (destination.stem + sidecar.name[len(source.stem):])
                if sidecar != new:
                    moves[sidecar] = new

    def index_for(folder: Path) -> MergedVideoInfo:
        """为目标 folder 复用事务内的合并索引，返回 MergedVideoInfo。"""
        path = folder / 'video_info.json'
        if path not in indexes:
            indexes[path] = MergedVideoInfo(info.output_dir, folder)
        return indexes[path]

    def move_stream_children(root: Path, quality: Quality, video_id: str) -> None:
        """将 root 下各版本断点迁入 quality/video_id 的目标路径；混合模式另按实际画质隔离，无返回值。"""
        destination = destination_folder(quality) / '.streams' / video_id
        if mixed:
            destination /= f'qn{quality.code}' if quality.code is not None else 'legacy'
        for child in root.iterdir():
            new = destination / child.name
            if child != new:
                moves[child] = new

    source_entries, paths = [], []
    try:
        for item in info.data['files']:
            check_cancel()
            quality = entry_quality(item)
            role = 'merged' if item['kind'] == 'merged' else 'part'
            source = info.resolve_file(item) if item.get('path') else None
            destination = destination_media(item, source, quality, role, was_mixed) if source else None
            variant_suffix = re.search(r':fmt[0-9a-f]{64}$', item['id'])
            raw_id = item['id'][:variant_suffix.start()] if variant_suffix else item['id']
            item.update(id=quality_entry_id(re.sub(r':qn[1-9]\d*$', '', raw_id), quality)
                        + (variant_suffix[0] if variant_suffix else ''),
                        quality_id=quality.code, quality_name=quality.name, quality_dir=suffix_for(quality))
            if source:
                add_media(source, destination)
            if role == 'merged':
                index_for(destination_folder(quality, role)).register(info, item, destination)
            else:
                source_entries.append(item)
                if destination:
                    paths.append((item, destination))
                if item.get('part') is not None:
                    stream_qualities.setdefault(f"{info.data['bvid']}_p{item['part']}", set()).add(quality)
        for folder in sorted(old_folders):
            validate_path(folder, info.output_dir)
            parsed = folder_quality(base, folder)
            if not folder.is_dir() or (parsed and parsed[1] == 'merged'):
                continue
            folder_mixed = folder == base.with_name(base.name + '_分P')
            for stream in (folder / '.streams').glob(f"{info.data['bvid']}_p*"):
                if not re.fullmatch(re.escape(info.data['bvid']) + r'_p[1-9]\d*', stream.name):
                    continue
                if folder_mixed:
                    for bucket in stream.iterdir():
                        match = re.fullmatch(r'qn([1-9]\d*)', bucket.name)
                        code = int(match[1]) if match else None
                        if not bucket.is_dir() or (not match and bucket.name != 'legacy') or code not in registry:
                            raise ToolError(f'无法确认混合目录断点的实际画质：{bucket}')
                        move_stream_children(bucket, registry[code], stream.name)
                else:
                    choices = stream_qualities.get(stream.name, {LEGACY_QUALITY})
                    quality = parsed[0] if parsed else next(iter(choices)) if len(choices) == 1 else LEGACY_QUALITY
                    move_stream_children(stream, quality, stream.name)
            for sidecar in folder.iterdir():
                owned = old_path.name == 'video_info.json' or info.data['bvid'] in sidecar.name
                if sidecar.is_file() and sidecar.suffix.lower() in ('.srt', '.ass', '.vtt') and sidecar not in known and owned:
                    if mixed != was_mixed:
                        raise ToolError(f'字幕未关联源视频，无法在切换模式时确定新文件名：{sidecar}')
                    quality = parsed[0] if parsed else LEGACY_QUALITY
                    destination = destination_folder(quality) / sidecar.name
                    if sidecar != destination:
                        moves[sidecar] = destination
                elif sidecar.is_file() and sidecar.suffix.lower() in ('.mp4', '.mkv') and sidecar not in known and owned and (not parsed or not parsed[1]):
                    print(f'未登记媒体保留原位，请补生成信息后整理：{sidecar}')
        # 分离保存的合并成品也随布局切换迁移；同一合集其他 BV 的条目和媒体保持原状。
        for folder in sorted(old_folders):
            parsed = folder_quality(base, folder)
            if not parsed or parsed[1] != 'merged' or not (folder / 'video_info.json').is_file():
                continue
            old_index = MergedVideoInfo(info.output_dir, folder)
            moved_ids, moved_results = set(), set()
            for item in old_index.data['files']:
                if item['bvid'] != info.data['bvid']:
                    continue
                source = old_index.resolve_file(item) if item.get('path') else None
                if source is None:
                    continue
                copy = deepcopy(item)
                quality = entry_quality(copy)
                destination = destination_media(copy, source, quality, 'merged', folder == base.with_name(base.name + '_合并'))
                if source == destination:
                    continue
                add_media(source, destination)
                index_for(destination.parent).register(info, copy, destination)
                moved_ids.add(item['id'])
            result_folder = naming.role_folder(target, '', True, 'merged')
            if folder != result_folder:
                for result in old_index.data['merge_results']:
                    if result['bvid'] != info.data['bvid']:
                        continue
                    copy = deepcopy(result)
                    copy['directory_base'] = target.name
                    index_for(result_folder).register_result(copy)
                    moved_results.add(result['id'])
            if moved_ids or moved_results:
                old_index.data['files'] = [item for item in old_index.data['files'] if item['id'] not in moved_ids]
                old_index.data['merge_results'] = [item for item in old_index.data['merge_results'] if item['id'] not in moved_results]
                obsolete[old_index.path] = old_index
        anchor = source_entries[0] if source_entries else original['files'][0] if original['files'] else None
        name = f"{info.data['bvid']}_video_info.json" if shared else old_path.name
        canonical = old_path
        if anchor is not None or shared:
            destinations = {destination_folder(entry_quality(item)) for item in source_entries}
            if target != base or name != old_path.name or old_path.parent not in destinations:
                canonical = destination_folder(entry_quality(anchor) if anchor else LEGACY_QUALITY) / name
            if canonical != old_path and canonical.exists() and canonical not in old_indexes:
                raise ToolError(f'源投稿记录已存在，不覆盖：{canonical}')
            for item, destination in paths:
                item['path'] = destination.relative_to(canonical.parent, walk_up=True).as_posix()
            info.data.update(files=source_entries, directory_base=target.name)
        info.data.update(schema_version=7, record_type='source', mix_quality=mixed)
        if info.data == original and not moves and not indexes and canonical == old_path:
            return
        info.path = canonical
        if target != base or mixed != was_mixed:
            info._quality_folders = {}
        records = [*(info.index_paths if persist_source else []), *indexes, *(path for path, index in obsolete.items() if index.has_records)]
        deletes = [path for path, index in obsolete.items() if not index.has_records]

        def commit() -> None:
            """保存目标合并索引、旧共享索引和全部源索引；异常由外层文件事务回滚，无参数及返回值。"""
            for index in [*indexes.values(), *(index for index in obsolete.values() if index.has_records)]:
                check_cancel()
                index.save()
            check_cancel()
            if persist_source:
                info.save()

        apply_changes(info.output_dir, list(moves.items()), records, commit, deletes)
    except BaseException:
        info.data, info.path = original, old_path
        info._index_paths, info._quality_folders = old_indexes, old_quality_folders
        raise
    for folder in old_folders:
        metadata.remove_empty_directories(folder, info.output_dir)
    if base.parent == target:
        metadata.remove_empty_directories(target, info.output_dir)


def organize_independent_merges(index: MergedVideoInfo, mix_quality: bool) -> set[Path]:
    """按 mix_quality 整理无源索引的 index 成品及结果，包括仅含失败的索引；保留来源快照，不创建源记录，返回索引路径集合。"""
    destinations = set()
    folder = index.path.parent
    subjects = list(index.data['files'])
    seen = {item['bvid'] for item in subjects}
    for result in index.data['merge_results']:
        if result['bvid'] not in seen:
            subjects.append(result)
            seen.add(result['bvid'])
    for item in subjects:
        result_only = 'source_expression' in item
        suffix = '_' + item.get('quality_dir', entry_quality(item).directory) + '_合并'
        mixed = item.get('mix_quality') is True and bool(item.get('base_stem'))
        if result_only:
            base_name, mixed = item['directory_base'], True
        elif mixed and folder.name.endswith('_合并'):
            base_name = folder.name[:-len('_合并')]
        elif folder.name.endswith(suffix):
            base_name = folder.name[:-len(suffix)]
        else:
            raise ToolError(f'独立合并目录无法确认基础名称：{folder}')
        info = metadata.VideoInfo(index.output_dir, folder, item['bvid'], item.get('video_title') or item.get('title'),
                                  item.get('total_parts'), shared=True)
        if info._index_paths:
            raise ToolError(f'合并目录内出现源索引，无法按独立成品整理：{info.path}')
        info.data.update(directory_base=base_name, mix_quality=mixed, source_url=item.get('source_url'),
                         uploader=deepcopy(item.get('uploader')), errors=[])
        organize_submission(info, mix_quality=mix_quality, persist_source=False)
        if not result_only:
            destinations.add(info.quality_directory(entry_quality(item), role='merged') / 'video_info.json')
        result_path = naming.role_folder(info.base_directory, '', True, 'merged') / 'video_info.json'
        if result_path.is_file():
            destinations.add(result_path)
    return destinations
