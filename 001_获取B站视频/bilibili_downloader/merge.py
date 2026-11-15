"""解析分 P 分组表达式，按指定清晰度执行无损拼接并汇总结果。"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import models
from . import naming
from . import library, provenance
from . import quality as qualities
from .config import Settings
from .errors import ToolError
from .metadata import VideoInfo, timestamp
from .merged_records import MergedVideoInfo
from .models import Quality
from .storage import output_directory, replace_file_transaction, validate_path, record_io_lock
from .activity_log import Activity, HtmlLog, progress_number
from . import runtime
from .errors import check_cancel
from . import variants


def parse_merge_group(expression: str) -> list[int]:
    """解析单组 expression（正整数、逗号及闭区间）；返回保留顺序和重复项的编号列表，非法或不足两段时报错。"""
    parts = []
    for token in expression.split(","):
        match = re.fullmatch(r"([0-9]+)(?:\s*-\s*([0-9]+))?", token.strip())
        if not match:
            raise ToolError(f"分 P 表达式无效：{token!r}，请使用正整数、逗号和范围（例如 1-5,8）")
        try:
            first = int(match[1])
            last = int(match[2]) if match[2] is not None else first
        except ValueError as exc:
            raise ToolError("分 P 编号过长，无法解析") from exc
        if first < 1 or last < first:
            raise ToolError(f"分 P 必须为正整数，范围起点不能大于终点：{token.strip()}")
        parts.extend(range(first, last + 1))
    if len(parts) < 2:
        raise ToolError("每组至少需要两个片段，重复分 P 计入片段数量")
    return parts


def merge_sequence_name(parts: list[int]) -> str:
    """将非空正整数列表 parts 转为文件名后缀；仅压缩相邻递增编号，返回保留顺序和重复项的 p 范围文本。"""
    segments = []
    first = last = parts[0]
    for part in parts[1:]:
        if part == last + 1:
            last = part
        else:
            segments.append(f"p{first}" + (f"-{last}" if last != first else ""))
            first = last = part
    segments.append(f"p{first}" + (f"-{last}" if last != first else ""))
    return "_".join(segments)


def record_merge_result(record: VideoInfo, *, status: str, source_parts: list[int] | None,
                        expression: str, quality: Quality | None = None,
                        assignments: dict[int, Quality] | None = None, error: str = '', started: str = '',
                        finished: str = '', occurrence: int = 1, destination: Path | None = None,
                        result_id: str | None = None, source_entries: dict[int, str] | None = None) -> Path:
    """把一次合并结果写入 record 基础合并目录，返回索引路径，不修改或创建媒体文件。

    status 为 completed/reused/failed/interrupted/skipped；source_parts 为请求顺序，非法表达式时用 None。
    expression 保存原始分组或“全部分 P”；quality 为已确认单档，assignments 保存各分 P 实际档位。
    error 为失败或跳过原因；started/finished 为 ISO 时间，空值采用当前时间；occurrence 为重复组序号。
    destination 为成功成品或 None；result_id 可用于幂等导入历史，未给时创建新的独立结果标识。
    source_entries 为逐P的准确来源条目标识，保留多编码版本选择。
    """
    parts = source_parts
    if parts is None and expression == '全部分 P':
        total = record.data.get('total_parts')
        parts = list(range(1, total + 1)) if type(total) is int and total > 0 else None
    if assignments is not None:
        available = assignments
    elif quality is not None:
        available = {item['part']: models.entry_quality(item) for item in record.data['files']
                     if item.get('kind') == 'part' and item.get('part') is not None
                     and models.entry_quality(item).code == quality.code}
    else:
        available = {}
    qualities = [{'part': part, 'quality_id': available[part].code, 'quality_name': available[part].name}
                 for part in dict.fromkeys(parts or []) if part in available]
    value = {
        'id': result_id or uuid4().hex, 'bvid': record.data['bvid'], 'video_title': record.data['title'],
        'source_url': record.data['source_url'], 'uploader': record.data.get('uploader'),
        'total_parts': record.data.get('total_parts'), 'directory_base': record.base_directory.name,
        'status': status, 'source_expression': expression, 'source_parts': parts, 'occurrence': occurrence,
        'quality_id': quality.code if quality else None, 'quality_name': quality.name if quality else None,
        'part_qualities': qualities, 'errors': [error] if error else [],
        'started': started or timestamp(), 'finished': finished or timestamp(),
        'file_name': destination.name if destination else None,
        'source_entries': [source_entries.get(part) for part in parts or []] if source_entries is not None else None,
    }
    with record_io_lock:
        index = MergedVideoInfo.for_results(record)
        index.register_result(value)
        index.save()
        return index.path


def record_merge_failure(record: VideoInfo, error: BaseException, **details: Any) -> None:
    """按 details 记录 record 的失败或中断；保存失败时明确报告并附注于原 error，保留原异常和旧成品；无返回值。"""
    try:
        record_merge_result(record, status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                            error=str(error) or '用户中止', **details)
    except (OSError, ToolError) as write_error:
        message = f'合并结果记录保存失败：{write_error}'
        error.add_note(message)
        print(message, file=sys.stderr, flush=True)


def merge_parts(settings: Settings, executable: str, record: VideoInfo,
                source_parts: list[int] | None = None, occurrence: int = 1, *, quality: Quality | None = None,
                activity: Activity | None = None, source_expression: str | None = None,
                source_entries: dict[int, str] | None = None) -> Path:
    """按 settings/executable 拼接 record，比较来源内容后按配置更新或复用，返回成品 Path 并更新可选 activity。

    source_parts 为至少两项的正整数列表，None 表示全部；occurrence 为重复序列的正整数序号。
    quality 为指定画质或 None（交互选择），activity 为当前组的 HTML 日志句柄或 None。
    source_expression 可保留原始分组表达式，None 时由 source_parts 生成；所有结局都保存到 merge_results。
    source_entries 可明确指定各P来源版本；None 时有多个本地版本则交互选择兼容方案。
    """
    if activity:
        activity.update('merging', quality=quality.name if quality else '', detail='正在校验分 P 并无损拼接')
    started = timestamp()
    expression = source_expression if source_expression is not None else ','.join(map(str, source_parts)) if source_parts is not None else '全部分 P'
    try:
        check_cancel()
        quality = quality if quality is not None else qualities.choose_merge_quality(record)
        if source_entries is None:
            source_entries = qualities.choose_local_sources(record, quality, source_parts)
        destination, reused, reason = _merge_parts(settings, executable, record, source_parts, occurrence, quality=quality,
                                                  activity=activity, source_entries=source_entries)
    except (Exception, KeyboardInterrupt) as exc:
        record_merge_failure(record, exc, source_parts=source_parts, expression=expression, quality=quality,
                             started=started, occurrence=occurrence, source_entries=source_entries)
        if activity:
            activity.update('interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed', detail=str(exc) or '用户中止')
        raise
    record_merge_result(record, status='reused' if reused else 'completed', source_parts=source_parts,
                        expression=expression, quality=quality, started=started, occurrence=occurrence,
                        destination=destination, source_entries=source_entries)
    if activity:
        activity.update('reused' if reused else 'completed', path=str(destination),
                        detail=reason if reused else reason + '；无损拼接完成，原分 P 和字幕已保留')
    return destination


def _merge_parts(settings: Settings, executable: str, record: VideoInfo,
                 source_parts: list[int] | None = None, occurrence: int = 1, *, quality: Quality | None = None,
                 activity: Activity | None = None, source_entries: dict[int, str] | None = None) -> tuple[Path, bool, str]:
    """按 record 拼接，返回目标 Path、是否复用及原因；settings 控制命名及覆盖策略，executable 指定 FFmpeg。

    source_parts 为至少两项的正整数顺序列表，保留重复；None 表示完整性校验后按序拼接全部分 P。
    occurrence 为该显式序列在本批次的正整数出现次数，用于区分重复输出；成功后保存完整来源顺序。
    quality 指定本地清晰度；None 时单档自动使用、多档交互确认，不跨档位补齐。
    activity 为可选网页活动，启用 FFmpeg 实时媒体时间和输出吞吐量采集。
    来源快照一致且成品有效时，settings.overwrite_merged 为 False 才复用；来源变化或未知时始终重新合并。
    失败或取消时恢复旧成品与索引；空目标不算已完成结果，仍重新生成。
    source_entries 按P限定准确来源条目；不使用其他编码或历史版本补齐。
    """
    if source_parts is not None and (len(source_parts) < 2 or any(type(part) is not int or part < 1 for part in source_parts)):
        raise ToolError("每组至少需要两个片段，分 P 编号必须为正整数")
    if type(occurrence) is not int or occurrence < 1:
        raise ToolError("拼接序列的出现次数必须为正整数")
    video_id = record.data["bvid"]
    quality = quality if quality is not None else qualities.choose_merge_quality(record)
    expected = ({page['part']: page['cid'] for page in record.data['current_parts']}
                if 'current_parts' in record.data else None)
    if expected is not None and source_parts is not None and set(source_parts) - expected.keys():
        raise ToolError(f'所选分 P 已不在当前投稿列表：{sorted(set(source_parts) - expected.keys())}')
    parts: dict[int, tuple[Path, dict[str, Any]]] = {}
    for item in record.data["files"]:
        if item.get("kind") != "part":
            continue
        if source_entries is not None and source_entries.get(item.get('part')) != item['id']:
            continue
        if models.entry_quality(item).code != quality.code:
            continue
        if source_parts is not None and item.get("part") not in source_parts:
            continue
        if expected is not None and item.get('part') not in expected:
            continue
        match = re.fullmatch(re.escape(video_id) + r"_p(\d+)(?::qn[1-9]\d*)?(?::fmt[0-9a-f]{64})?", item["id"])
        if not match or item.get("part") != int(match[1]):
            raise ToolError("下载记录中的分 P 标识无效")
        variant = item['id'].rsplit(':fmt', 1)[1] if ':fmt' in item['id'] else ''
        if (item['id'] != variants.source_id(f"{video_id}_p{item['part']}", quality, variant)
                or (variant and item.get('formats') and variants.variant_id(item['formats']) != variant)):
            raise ToolError("下载记录中的分 P 清晰度标识无效")
        part = int(match[1])
        if expected is not None and item.get('source_cid') != expected[part]:
            raise ToolError(f'第 {part} P 本地来源 CID 与当前版本不一致，需先完成更新下载')
        if item.get("status") != "completed":
            raise ToolError(f"第 {part} P 尚未完成")
        source = record.resolve_file(item)
        if not source.is_file() or not source.stat().st_size:
            raise ToolError(f"第 {part} P 文件不存在或为空：{source}")
        if part in parts:
            raise ToolError(f"第 {part} P 存在重复记录")
        parts[part] = (source, item)
    if source_parts is None:
        if len(parts) < 2:
            raise ToolError(f"{video_id} 至少需要两个已下载分 P 才能拼接")
        total = record.data.get("total_parts")
        if total is not None and (type(total) is not int or total < 1):
            raise ToolError("下载记录中的分 P 总数不一致或无效")
        if total is None:
            print("旧下载记录没有总集数，无法确认最后一 P 是否已下载；将拼接目前连续的分 P。")
            total = max(parts)
        if sorted(parts) != list(range(1, total + 1)):
            raise ToolError(f"分 P 不完整：需要 P1 至 P{total}，实际已有 {sorted(parts)}")
        sequence = sorted(parts)
    else:
        missing = sorted(set(source_parts) - parts.keys())
        if missing:
            raise ToolError(f"所选分 P 尚未下载：{missing}")
        sequence = source_parts
    ordered = [parts[number] for number in sequence]
    first_signature = variants.compatibility(ordered[0][1].get('formats'))
    if any(variants.compatibility(item.get('formats')) != first_signature for _, item in ordered[1:]):
        raise ToolError("各分 P 的编码或分辨率不同，无法直接无损拼接")
    snapshot = provenance.source_snapshot(ordered)
    library.cache_source_fingerprints(settings, record, ordered)
    title = record.data["title"]
    folder = record.quality_directory(quality, role='merged')
    merged_index = MergedVideoInfo(record.output_dir, folder)
    extension = ".mp4" if all(source.suffix.lower() == ".mp4" for source, _ in ordered) else ".mkv"
    suffix = ""
    if source_parts is not None:
        suffix = "_" + merge_sequence_name(sequence) + (f"_{occurrence}" if occurrence > 1 else "")
    # 序列后缀独立追加，不经过 video_stem 的标识截断，以免不同顺序映射到同名文件。
    base_stem = naming.video_stem(settings, title, video_id) + suffix
    destination = folder / (naming.quality_stem(base_stem, record.quality_suffix(quality), record.mix_quality) + extension)
    entry_id = models.quality_entry_id(f'{video_id}:merged{suffix}', quality)
    previous = next((item for item in merged_index.data['files'] if item['id'] == entry_id), None)
    # 已登记的同容器成品沿用原文件名，投稿改名只更新信息，不改变来源内容或创建同标识的第二份文件。
    if previous and previous.get('path') and Path(previous['path']).suffix.lower() == extension:
        destination = merged_index.resolve_file(previous)
        base_stem = previous.get('base_stem') or (destination.stem if not record.mix_quality else base_stem)
    same_sources = (previous is not None and previous.get('source_parts') == list(sequence)
                    and previous.get('source_snapshot') == snapshot)
    reason = '来源变化，重新合并' if previous and previous.get('source_snapshot') else '成品缺失或来源未知，重新合并'
    validate_path(destination, record.output_dir)
    try:
        destination.stat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ToolError(f"拼接目标名称或路径无效，请检查长度和权限：{destination}：{exc}") from exc
    else:
        if not destination.is_file():
            raise ToolError(f"合并目标不是普通文件：{destination}")
        valid = (previous is not None and previous.get('status') == 'completed'
                 and previous.get('path') == destination.name
                 and previous.get('size_bytes') == destination.stat().st_size and destination.stat().st_size > 0)
        if same_sources and valid:
            reason = '来源相同，按配置强制重合并' if settings.overwrite_merged else '来源相同，复用现有合并成品'
            if not settings.overwrite_merged:
                print(f'{reason}：{destination}')
                return destination, True, reason
        elif same_sources:
            reason = '成品缺失或无效，重新合并'
    print(f'{reason}：{destination}', flush=True)
    with output_directory(folder, record.output_dir), tempfile.TemporaryDirectory(prefix=".merge-", dir=folder) as temporary:
        temporary_dir = Path(temporary)
        playlist = temporary_dir / "parts.ffconcat"
        lines = ["file '" + source.resolve().as_posix().replace("'", "'\\''") + "'" for source, _ in ordered]
        playlist.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        output = temporary_dir / ("merged" + extension)
        command = [executable, "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(playlist), "-c", "copy", str(output)]
        options = {}
        if activity:
            durations = [progress_number(item.get('duration_seconds')) for _, item in ordered]
            duration = sum(durations) if all(value is not None and value > 0 for value in durations) else None
            activity.start_merge(duration, sum(source.stat().st_size for source, _ in ordered))
            command[1:1] = ['-progress', 'pipe:1', '-nostats', '-stats_period', str(settings.log_progress_interval)]
            block: dict[str, str] = {}

            def observe_progress(line: str) -> None:
                """解析 FFmpeg 单行 line，在 progress 标记处提交完整指标块；无返回值。"""
                key, separator, value = line.strip().partition('=')
                if separator:
                    block[key] = value
                    if key == 'progress':
                        activity.merge_progress(block)
                        block.clear()

            options['stdout_callback'] = observe_progress
        result = runtime.run_process(command, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **options)
        check_cancel()
        if result.returncode != 0 or not output.is_file() or not output.stat().st_size:
            raise ToolError("拼接失败，原分 P 已保留：\n" + "\n".join(result.stderr.splitlines()[-10:]))
        if "non-monoton" in result.stderr.lower():
            print("FFmpeg 报告时间戳不连续，请检查拼接点的播放效果。")
        library.cache_source_fingerprints(settings, record, ordered)
        merged = merged_index.register(record, {
            'id': entry_id, 'kind': 'merged', 'part': None,
            'title': title, 'status': 'pending', 'errors': [], 'formats': ordered[0][1]['formats'],
            'processing': 'stream copy', 'source_parts': list(sequence), 'quality_id': quality.code,
            'quality_name': quality.name, 'quality_dir': record.quality_suffix(quality), 'source_snapshot': snapshot,
            'base_stem': base_stem, 'mix_quality': record.mix_quality,
            'source_entries': [item['id'] for _, item in ordered],
        }, destination)

        def commit() -> None:
            """持久化合并完成状态并从最终文件探测媒体参数；失败由事务回滚，无参数及返回值。"""
            merged_index.complete(merged, destination, executable)

        replaced = replace_file_transaction(record.output_dir, output, destination, [merged_index.path], commit)
    if merged['errors']:
        print(f"合并视频信息提示：{destination}：" + '；'.join(merged['errors']))
    print(f"{'同名合并结果已覆盖' if replaced else '分 P 已拼接'}（原编码）：{destination}")
    return destination, False, reason


def merge_groups(settings: Settings, executable: str, record: VideoInfo, expression: str,
                 *, assignments: dict[int, Quality] | None = None, quality: Quality | None = None,
                 journal: HtmlLog | None = None, task: str = '', source_entries: dict[int, str] | None = None) -> int:
    """按 expression 分组拼接 record；settings 控制命名，executable 指定 FFmpeg，返回 0/2。

    assignments 非空时仅使用本清单任务各分P选定的画质，每组必须同档，不触发交互；None 时按现有菜单选择。
    quality 可预先指定普通链接画质以避免工作线程交互；journal/task 为可选日志和任务标识。
    source_entries 为在线下载已选定的逐P来源版本；离线 None 时使用本地版本选择。
    """
    quality = quality if assignments is None else None
    occurrences: dict[tuple[int, ...], int] = {}
    results = []
    failed = 0
    for index, group in enumerate(expression.split(";"), 1):
        check_cancel()
        label = f"第 {index} 组 [{group.strip()}]"
        activity = journal.merge_activity(record.data['title'], task=task or record.data['bvid'], part=label) if journal else None
        parts, selected_quality, delegated = None, quality, False
        started = timestamp()
        try:
            parts = parse_merge_group(group)
            if assignments is None and quality is None:
                quality = selected_quality = qualities.choose_merge_quality(record)
            if assignments is not None:
                missing = sorted(set(parts) - assignments.keys())
                if missing:
                    raise ToolError(f'本任务未分配画质的分P：{missing}')
                choices = {assignments[part].code: assignments[part] for part in parts}
                if len(choices) != 1:
                    raise ToolError('本组分P使用不同画质，合并要求实际档位一致')
                selected_quality = next(iter(choices.values()))
            key = tuple(parts)
            occurrences[key] = occurrences.get(key, 0) + 1
            options = {'activity': activity} if activity else {}
            if source_entries is not None:
                options['source_entries'] = source_entries
            delegated = True
            destination = merge_parts(settings, executable, record, parts, occurrences[key], quality=selected_quality,
                                      source_expression=group.strip(), **options)
            results.append(f"{label} 成功：{destination}")
        except (Exception, KeyboardInterrupt) as exc:
            if not delegated:
                record_merge_failure(record, exc, source_parts=parts, expression=group.strip(),
                                     quality=selected_quality, assignments=assignments, started=started)
            if activity:
                activity.update('interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed', detail=str(exc))
            if not isinstance(exc, (OSError, ToolError)):
                raise
            failed += 1
            results.append(f"{label} 失败：{exc}")
    print(f"\n分组拼接完成：成功 {len(results) - failed} 组，失败 {failed} 组。")
    for result in results:
        print(result)
    return 2 if failed else 0


def merge_plans(settings: Settings, executable: str, record: VideoInfo, plans: list[variants.MergePlan], *,
                full: bool = False, journal: HtmlLog | None = None, task: str = '') -> int:
    """用 settings/executable 执行 record 的 plans，full 表示全量；journal/task 记录逐组结果，返回0或2。"""
    occurrences, failed = {}, 0
    for index, plan in enumerate(plans, 1):
        check_cancel()
        label = '全部分 P' if full else f'第 {index} 组 [{plan.expression}]'
        activity = journal.merge_activity(record.data['title'], task=task, part=label) if journal else None
        delegated, quality, sources = False, None, None
        try:
            if plan.error:
                raise ToolError(plan.error)
            sources = variants.find_sources(record, plan.choices)
            quality = next(iter(plan.choices.values())).quality
            sequence = tuple(plan.parts)
            occurrences[sequence] = occurrences.get(sequence, 0) + 1
            delegated = True
            merge_parts(settings, executable, record, None if full else plan.parts, occurrences[sequence],
                        quality=quality, source_entries=sources, activity=activity, source_expression=plan.expression)
        except (OSError, ToolError) as exc:
            failed += 1
            if not delegated:
                record_merge_failure(record, exc, source_parts=plan.parts, expression=plan.expression, quality=quality,
                                     assignments={part: choice.quality for part, choice in plan.choices.items()},
                                     source_entries=sources)
            if activity:
                activity.update('failed', detail=str(exc))
            print(f'{label} 拼接失败：{exc}', file=sys.stderr, flush=True)
    return 2 if failed else 0
