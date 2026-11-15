"""管理分目录或混合画质的 v8 源索引，聚合投稿视图并事务化迁移旧记录。"""

from copy import deepcopy
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .errors import ToolError
from .models import entry_quality
from . import variants
from . import identity
from .storage import apply_changes, record_io_lock, validate_path

if TYPE_CHECKING:
    from .metadata import VideoInfo


def index_paths(output_dir: Path, bvid: str) -> list[Path]:
    """扫描 output_dir 中 bvid 的普通和合集源索引；排除合并索引及链接，返回排序后的绝对路径列表。"""
    from .metadata import read_json

    candidates = set(output_dir.rglob('video_info.json')) | set(output_dir.rglob(f'{bvid}_video_info.json'))
    result = []
    for path in sorted(candidates):
        if path.is_symlink() or path.parent.is_symlink() or path.parent.is_junction():
            continue
        try:
            data = read_json(path)
            if data.get('record_type') == 'merged':
                continue
            identifier = identity.source_id(data)
        except ToolError:
            if bvid in path.name or bvid in path.parent.name:
                raise
            continue
        if identifier == bvid:
            validate_path(path, output_dir)
            result.append(path.resolve())
    return result


def load_views(info: 'VideoInfo', paths: list[Path]) -> None:
    """将同一目录组的 paths 聚合到 info；统一内存相对路径，拒绝重复条目、重复画质目录和投稿信息冲突，无返回值。"""
    from .metadata import VideoInfo

    files, seen, folders = [], set(), {}
    with record_io_lock:
        for path in paths:
            source = VideoInfo(info.output_dir, path.parent, info.data['source_id'], info.data['title'], shared=path.name != 'video_info.json')
            if source.base_directory != info.base_directory:
                raise ToolError(f"{info.data['source_id']} 存在多个投稿目录，请整理后重试")
            if source.mix_quality != info.mix_quality:
                raise ToolError(f'同一投稿存在不同存放模式的索引：{path}')
            for key in ('source_type', 'source_id', 'title', 'source_url', 'total_parts', 'current_parts', 'season_id'):
                if source.data.get(key) != info.data.get(key):
                    raise ToolError(f'同一投稿各目录的 {key} 信息冲突：{path}')
            if source.data.get('schema_version') in (5, 6, 7, 8):
                for item in source.data['files']:
                    code = item.get('quality_id')
                    if code in folders and folders[code] != path.parent:
                        raise ToolError(f'同一投稿的画质存在多个目录：{path}')
                    folders[code] = path.parent
            for item in source.data['files']:
                if item['id'] in seen:
                    raise ToolError(f"同一投稿存在重复的分 P 或画质记录：{item['id']}")
                seen.add(item['id'])
                entry = deepcopy(item)
                if entry.get('path'):
                    entry['path'] = source.resolve_file(item).relative_to(info.path.parent, walk_up=True).as_posix()
                files.append(entry)
    info.data['files'] = files
    for key in ('quality_id', 'quality_name', 'quality_dir'):
        info.data.pop(key, None)
    info._index_paths = set(paths)
    info._quality_folders = folders


def documents(info: 'VideoInfo') -> dict[Path, dict[str, Any]]:
    """按 info 的存放模式构造 v8 文档并同步实际文件名；未开始条目使用分配目录，返回路径到文档的映射，不写盘。"""
    from .metadata import timestamp

    grouped: dict[Path, list[dict[str, Any]]] = {}
    base = info.base_directory
    for item in info.data['files']:
        if item['kind'] == 'merged':
            raise ToolError('混放的旧合并记录须先拆分，不能写入源目录索引')
        source = info.resolve_file(item) if item.get('path') else None
        folder = source.parent if source is not None else info.quality_directory(entry_quality(item))
        validate_path(folder, info.output_dir)
        if (folder.parent != base.parent or not folder.name.startswith(base.name + '_')
                or not folder.name.endswith('_分P')):
            raise ToolError(f'源媒体须先整理到投稿对应的分 P 目录：{folder}')
        entry = deepcopy(item)
        if source is not None:
            entry.update(path=source.name, file_name=source.name, stem=source.stem)
        if info.mix_quality:
            if folder != base.with_name(base.name + '_分P'):
                raise ToolError(f'混合画质媒体尚未整理到公共目录：{folder}')
            entry['quality_dir'] = info.quality_suffix(entry_quality(item))
        else:
            if folder == base.with_name(base.name + '_分P'):
                raise ToolError(f'分画质媒体尚未拆分目录：{folder}')
            entry['quality_dir'] = folder.name[len(base.name) + 1:-len('_分P')]
        grouped.setdefault(folder / info.path.name, []).append(entry)
    if not grouped:
        grouped[info.path] = []
    result, quality_folders = {}, {}
    for path, files in grouped.items():
        codes = {item.get('quality_id') for item in files}
        if len(codes) > 1 and not info.mix_quality:
            raise ToolError(f'同一源目录包含多个画质，无法独立登记：{path}')
        data = deepcopy(info.data)
        for item in files:
            if item.get('formats'):
                item['format_variant'] = variants.variant_id(item['formats'])
        data.update(schema_version=8, record_type='source', mix_quality=info.mix_quality, files=files, updated_at=timestamp())
        for key in ('quality_id', 'quality_name', 'quality_dir'):
            data.pop(key, None)
        if files:
            data['directory_base'] = base.name
            code = files[0].get('quality_id')
            if code in quality_folders and quality_folders[code] != path.parent:
                raise ToolError(f'同一投稿的画质存在多个目录：{path}')
            quality_folders[code] = path.parent
            if not info.mix_quality:
                data.update(quality_id=files[0].get('quality_id'),
                            quality_name=files[0].get('quality_name'), quality_dir=files[0]['quality_dir'])
        result[path] = data
    return result


def save_view(info: 'VideoInfo') -> None:
    """原子保存 info 的全部目录索引并核对，成功后移除旧跨目录索引；失败恢复磁盘及内存，无返回值。"""
    from .metadata import atomic_text, read_json, timestamp
    import json

    original, old_path = deepcopy(info.data), info.path
    old_indexes, old_folders = set(info._index_paths), dict(info._quality_folders)
    docs = documents(info)
    for path in docs:
        validate_path(path, info.output_dir)
        if path.exists() and path not in old_indexes:
            raise ToolError(f'源目录索引已存在，拒绝覆盖未加载的记录：{path}')
    paths = sorted(docs)
    anchor = old_path if old_path in docs else paths[0]
    deletes = sorted(old_indexes - docs.keys())

    def commit() -> None:
        """写入已准备的目录文档并逐份核对；所有文件由外层事务恢复，无参数及返回值。"""
        for path, data in docs.items():
            atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        for path, data in docs.items():
            if read_json(path) != data:
                raise ToolError(f'源目录索引写入核对失败：{path}')

    try:
        with record_io_lock:
            apply_changes(info.output_dir, [], paths, commit, deletes)
        for item in info.data['files']:
            if item.get('formats'):
                item['format_variant'] = variants.variant_id(item['formats'])
            source = info.resolve_file(item) if item.get('path') else None
            if source is not None:
                item.update(path=source.relative_to(anchor.parent, walk_up=True).as_posix(),
                            file_name=source.name, stem=source.stem)
            folder = source.parent if source is not None else info.quality_directory(entry_quality(item))
            if not info.mix_quality:
                item['quality_dir'] = folder.name[len(info.base_directory.name) + 1:-len('_分P')]
        info.path = anchor
        if info.data['files']:
            info.data['directory_base'] = docs[anchor]['directory_base']
        info.data.update(schema_version=8, record_type='source', mix_quality=info.mix_quality, updated_at=timestamp())
        for key in ('quality_id', 'quality_name', 'quality_dir'):
            info.data.pop(key, None)
        info._index_paths = set(paths)
        info._quality_folders = {item.get('quality_id'): path.parent for path, data in docs.items() for item in data['files']}
    except BaseException:
        info.data, info.path = original, old_path
        info._index_paths, info._quality_folders = old_indexes, old_folders
        raise
