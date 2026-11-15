"""管理合并目录的 video_info.json，分开保存实际成品和成功、失败、中断等合并结果。"""

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from . import metadata, naming
from .errors import ToolError
from .storage import validate_path


class MergedVideoInfo(metadata.VideoInfo):
    """合并目录索引；复用媒体探测和属性记录接口，不参与源投稿定位或下载续传。"""

    def __init__(self, output_dir: Path, folder: Path) -> None:
        """从 output_dir 边界内 folder 加载索引；缺文件时创建空索引，格式损坏时报错，无返回值。"""
        self.output_dir = output_dir.resolve()
        self.path = folder.resolve() / 'video_info.json'
        validate_path(folder / 'video_info.json', self.output_dir)
        now = metadata.timestamp()
        self.data = metadata.read_json(self.path) if self.path.exists() else {
            'schema_version': 1, 'record_type': 'merged', 'created_at': now, 'updated_at': now, 'files': [],
        }
        files = self.data.get('files')
        if (self.data.get('schema_version') != 1 or self.data.get('record_type') != 'merged'
                or not isinstance(files, list) or any(not isinstance(item, dict) for item in files)):
            raise ToolError(f'合并视频索引格式无效：{self.path}')
        ids = [item.get('id') for item in files]
        if (any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids)
                or any(item.get('kind') != 'merged' or not isinstance(item.get('bvid'), str)
                       or not isinstance(item.get('errors'), list) for item in files)):
            raise ToolError(f'合并视频索引条目无效：{self.path}')
        results = self.data.setdefault('merge_results', [])
        if (not isinstance(results, list) or any(not isinstance(item, dict) for item in results)
                or any(not isinstance(item.get('id'), str) or not item['id']
                       or not isinstance(item.get('bvid'), str) or not isinstance(item.get('errors'), list)
                       or item.get('status') not in {'completed', 'reused', 'failed', 'interrupted', 'skipped'} for item in results)
                or len({item['id'] for item in results}) != len(results)):
            raise ToolError(f'合并结果记录无效：{self.path}')

    @classmethod
    def for_results(cls, source: metadata.VideoInfo) -> 'MergedVideoInfo':
        """取得 source 基础合并目录的统一结果索引；混合画质失败无单一档位，返回不带画质后缀的索引。"""
        return cls(source.output_dir, naming.role_folder(source.base_directory, '', True, 'merged'))

    @property
    def has_records(self) -> bool:
        """返回索引是否仍有成品或合并结果，防止迁移时删除只含失败记录的索引；无参数。"""
        return bool(self.data['files'] or self.data['merge_results'])

    def register_result(self, result: dict[str, Any]) -> None:
        """按稳定 id 登记独立的 result 合并结果；重复导入同一结果时更新而不追加，不修改成品条目；无返回值。"""
        previous = next((item for item in self.data['merge_results'] if item['id'] == result['id']), None)
        if previous is None:
            self.data['merge_results'].append(deepcopy(result))
        else:
            previous.update(deepcopy(result))

    def save(self) -> None:
        """原子保存此合并目录的索引，保留其他投稿条目；无参数及返回值。"""
        validate_path(self.path, self.output_dir)
        self.data['updated_at'] = metadata.timestamp()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        metadata.atomic_text(self.path, json.dumps(self.data, ensure_ascii=False, indent=2) + '\n')

    def resolve_file(self, entry: dict[str, Any]) -> Path:
        """解析 entry 的合并媒体路径并限制在索引所在目录，返回绝对 Path。"""
        path = super().resolve_file(entry)
        if path.parent != self.path.parent:
            raise ToolError(f'合并媒体超出索引所在目录：{path}')
        return path

    def register(self, source: metadata.VideoInfo, item: dict[str, Any], destination: Path | None) -> dict[str, Any]:
        """复制 source 的合并 item 到本索引；destination 为新媒体路径或 None，返回独立的索引条目。"""
        entry = deepcopy(item)
        bvid = source.data['bvid']
        if not entry['id'].startswith(bvid + ':'):
            entry['id'] = bvid + ':' + entry['id']
        entry.update(bvid=bvid, video_title=source.data['title'], uploader=deepcopy(source.data.get('uploader')),
                     source_url=source.data['source_url'], total_parts=source.data.get('total_parts'), kind='merged')
        if 'source_parts' not in entry:
            entry['source_parts'] = None
            entry['errors'] = [*entry.get('errors', []), '旧记录未保存来源分P顺序']
        if destination is not None:
            validate_path(destination, self.output_dir)
            if destination.parent.resolve() != self.path.parent:
                raise ToolError(f'合并文件必须位于索引所在目录：{destination}')
            entry.update(path=destination.name, file_name=destination.name, stem=destination.stem)
        existing = next((value for value in self.data['files'] if value['id'] == entry['id']), None)
        if existing is not None:
            if existing.get('path') != entry.get('path'):
                raise ToolError(f'合并结果标识冲突：{entry["id"]}')
            existing.update(entry)
            return existing
        if entry.get('path') and any(value.get('path') == entry['path'] for value in self.data['files']):
            raise ToolError(f'合并结果文件名冲突：{entry["path"]}')
        self.data['files'].append(entry)
        return entry

    def refresh_local(self, executable: str) -> None:
        """用 executable 刷新实际成品属性，保留 merge_results 中的失败及历史结果；无需源分 P 或登录，无返回值。"""
        for item in self.data['files']:
            if not item.get('path'):
                continue
            try:
                self.record_file(item, self.resolve_file(item))
                self.enrich_file(item, executable)
            except (OSError, ToolError) as exc:
                item.update(status='failed', errors=[str(exc)])
        self.save()
