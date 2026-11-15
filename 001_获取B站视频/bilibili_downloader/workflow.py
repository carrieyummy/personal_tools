"""在主线程准备多个投稿、确认或自动选择画质，调度共享下载池并按任务收尾拼接。"""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event
from typing import Any

from . import auth
from . import downloader as transfer
from . import library
from . import media
from . import merge as merging
from . import models
from . import quality as qualities
from . import subtitles
from .config import Settings
from .errors import DownloadCancelled, ToolError, check_cancel
from .models import Quality, SubtitleTrack, VideoTarget
from .manifest import DownloadRequest
from .scheduler import run_jobs
from .activity_log import Activity, HtmlLog
from . import runtime
from . import variants
from . import identity


def print_video_info(downloader: Any, info: dict[str, Any], tracks: list[SubtitleTrack], subtitle_error: str | None) -> None:
    """显示 downloader 解析的 info 画质和 tracks 字幕；subtitle_error 区分接口失败和确实无字幕。"""
    print(f"\n视频：{info['title']} [{info['id']}]", flush=True)
    downloader.list_formats(info)
    for fmt in media.chosen_formats(info):
        print(f"已选：{fmt.get('format_id')} | {fmt.get('width', '-')}x{fmt.get('height', '-')} | {fmt.get('fps', '-')} fps | 视频 {fmt.get('vcodec')} | 音频 {fmt.get('acodec')}")
    if subtitle_error:
        print(f"字幕列表未完成：{subtitle_error}")
    elif not tracks:
        print("平台没有符合配置的可用字幕；仍会保存视频。")
    for track in tracks:
        print(f"字幕：{track.language} | {track.name} | {'AI' if track.is_ai else '人工'}")


def extract_target(downloader: Any, target: VideoTarget, context: Any = None, settings: Settings | None = None) -> dict[str, Any]:
    """查询 target 的单分P原始格式；downloader 处理投稿，番剧经 context/settings 调 PGC，不下载或自动选流，返回信息。"""
    if target.episode_id is not None:
        from . import bangumi

        return bangumi.extract_info(context, downloader, target, settings)
    info = downloader.extract_info(target.url, download=False, process=False)
    if not info or info.get("_type", "video") != "video":
        raise ToolError("链接未解析为单个视频分 P")
    return info


@dataclass
class Submission:
    """投稿任务：metadata/targets/request 为原站信息、目标P及输入；ceiling 是原有上限，selected_rank 是关联季逐集档位排名。

    selected_rank 为正整数，None 表示未选择或跳过；是否使用排名由 request.quality_group 决定，jobs/merge_plans 保存方案。
    """

    metadata: dict[str, Any]
    targets: list[VideoTarget]
    request: DownloadRequest
    assignments: dict[int, Quality] = field(default_factory=dict)
    ceiling: Quality | None = None
    failures: list[str] = field(default_factory=list)
    downloaded: set[int] = field(default_factory=set)
    saved_ids: set[str] = field(default_factory=set)
    activity: Activity | None = None
    part_activities: dict[int, Activity] = field(default_factory=dict)
    log_task: str = ''
    jobs: list[PartJob] = field(default_factory=list)
    merge_plans: list[variants.MergePlan] | None = None
    selected_rank: int | None = None

    @property
    def source_id(self) -> str:
        """返回当前任务的真实 BV 或番剧 ss/ep 来源标识，无参数。"""
        return identity.source_id(self.metadata)


@dataclass
class SubmissionCatalog:
    """准备阶段查询结果：catalogs 为逐P档位，variant_catalogs/raw_defaults 为兼容方案和默认流。

    initial_sizes 保存逐P逐档的版本及大小，use_merge 标记是否规划兼容拼接；不缓存原始播放地址或持久化画质选择。
    """

    catalogs: dict[int, dict[int, Quality]]
    variant_catalogs: dict[int, dict[int, dict[tuple, variants.StreamChoice]]]
    raw_defaults: dict[int, dict[int, variants.StreamChoice]]
    initial_sizes: dict[int, dict[int, dict[str, Any]]]
    use_merge: bool


@dataclass
class PartJob:
    """下载版本：submission/target/quality/choice 锁定目标，activity 为日志，size_info 保存去重身份及完整文件大小。"""

    submission: Submission
    target: VideoTarget
    quality: Quality
    choice: variants.StreamChoice | None = None
    activity: Activity | None = None
    actual_variant: str = ''
    saved_id: str = ''
    size_info: dict[str, Any] = field(default_factory=dict)
    known_size_formats: list[dict[str, Any]] | None = None  # 只有与此完整格式签名一致时才沿用准备阶段读到的实际大小。


@dataclass(frozen=True)
class PartPayload:
    """工作线程输入：job/info/tracks 为待下载数据，snapshot 为认证快照，errors 为字幕查询错误。"""

    job: PartJob
    info: dict[str, Any]
    tracks: list[SubtitleTrack]
    snapshot: auth.AuthSnapshot
    errors: list[str]


@dataclass(frozen=True)
class PartResult:
    """分P结果：saved_id/errors/path 为记录、错误和成品路径，size_info 将预计及实际大小同步给所有复用任务。"""

    saved_id: str
    errors: list[str]
    path: str = ''
    size_info: dict[str, Any] = field(default_factory=dict)


def job_key(job: PartJob) -> tuple:
    """返回 job 的来源、P、EP、CID、画质及实际格式版本，供传输与大小汇总使用同一去重口径。"""
    return (job.submission.source_id, job.target.part, job.target.episode_id, job.target.cid,
            job.quality.code, job.actual_variant)


def size_entries(submissions: list[Submission]) -> list[dict[str, Any]]:
    """取得 submissions 的大小条目供命令行汇总；有日志时沿用原条目，无日志时包含已规划版本及未解析目标。"""
    entries = []
    for submission in submissions:
        if submission.activity:
            journal = submission.activity.journal
            with journal.lock:
                entries.extend(dict(entry) for entry in journal.run['entries']
                               if entry['kind'] == '下载' and entry['task'] == submission.log_task)
        else:
            entries.extend({'kind': '下载', **job.size_info} for job in submission.jobs)
            covered = {job.target.part for job in submission.jobs}
            entries.extend({'kind': '下载', 'download_key': [submission.source_id, target.part, target.episode_id,
                                                            target.cid, None, '']}
                           for target in submission.targets if target.part not in covered)
    return entries


def ranked_quality(options: dict[int, Quality], rank: int) -> Quality:
    """从 options 按平台画质编号降序取正整数排名 rank；返回 Quality，排名不存在时报错。"""
    ordered = sorted(options.values(), key=lambda quality: quality.code, reverse=True)
    if rank > len(ordered):
        raise ToolError(f'画质排名 {rank} 不存在，实际可下载档位数为 {len(ordered)}')
    return ordered[rank - 1]


def mark_failure(settings: Settings, job: PartJob, error: BaseException | str) -> None:
    """按 settings 为 job 的未完成记录标记 error；取消标为 interrupted，已完成文件保持完成，无返回值。"""
    with library.video_transaction(settings, job.submission.source_id) as record:
        item = record.entry(f'{job.submission.source_id}_p{job.target.part}', part=job.target.part, quality=job.quality,
                            variant=job.choice.variant if job.choice else '')
        if item['status'] != 'completed':
            item.update(status='interrupted' if isinstance(error, (KeyboardInterrupt, DownloadCancelled)) else 'failed',
                        errors=[str(error) or '用户中止'])
            record.save()


def register_pending(settings: Settings, submission: Submission) -> None:
    """按 settings 为 submission 的全部下载版本登记 pending 条目和格式；保留已有状态，无返回值。"""
    with library.video_transaction(settings, submission.source_id) as record:
        for job in submission.jobs:
            item = record.entry(f'{submission.source_id}_p{job.target.part}', part=job.target.part, quality=job.quality,
                                variant=job.choice.variant if job.choice else '')
            if job.choice and not item['formats']:
                item.update(formats=list(job.choice.formats), format_variant=job.choice.variant)
        record.save()


def plan_groups(submission: Submission, catalogs: dict[int, dict[int, dict[tuple, variants.StreamChoice]]],
                defaults: dict[int, variants.StreamChoice], ceiling: Quality) -> None:
    """为 submission 逐组规划兼容版本；catalogs/defaults 为候选及逐P默认档，ceiling 限制兼容搜索范围；返回 None。"""
    selection = submission.request.merge_parts.strip()
    groups = selection.split(';') if selection not in ('', '-1', '0') else ['全部分 P']
    submission.merge_plans = []
    needed: dict[tuple[int, int, str], variants.StreamChoice] = {}
    covered = set()
    for expression in groups:
        plan = variants.MergePlan(expression.strip(), None)
        submission.merge_plans.append(plan)
        try:
            plan.parts = ([target.part for target in submission.targets] if selection == '0'
                          else merging.parse_merge_group(expression))
            if len(plan.parts) < 2:
                raise ToolError('每组至少需要两个片段')
            plan.choices = qualities.common_choice(catalogs, plan.parts, ceiling)
        except ToolError as exc:
            plan.error = str(exc)
            plan.choices = {part: defaults[part] for part in dict.fromkeys(plan.parts or []) if part in defaults}
        for part, choice in plan.choices.items():
            covered.add(part)
            needed[part, choice.quality.code, choice.variant] = choice
    for part, choice in defaults.items():
        if part not in covered:
            needed[part, choice.quality.code, choice.variant] = choice
    targets = {target.part: target for target in submission.targets}
    submission.jobs = [PartJob(submission, targets[part], choice.quality, choice)
                       for (part, _, _), choice in needed.items()]


def query_submission(context: Any, settings: Settings, submission: Submission) -> SubmissionCatalog:
    """通过 context/settings 准备 submission 归档并查询各P可下载档位及大小；逐P记录错误，返回选流目录，不弹菜单。"""
    metadata, targets = submission.metadata, submission.targets
    record = library.get_video_info(settings, submission.source_id, metadata['title'], targets[0].total_parts,
                                   shared_folder=library.prepare_collection(settings, metadata))
    library.update_source_info(record, metadata)
    record.save()
    catalogs, variant_catalogs, raw_defaults, initial_sizes = {}, {}, {}, {}
    selection = submission.request.merge_parts.strip()
    use_merge = settings.priority == 'merge' and (selection not in ('', '-1', '0')
                 or (selection == '0' and submission.request.all_parts and len(targets) > 1))
    if submission.request.title:
        print(f'\n清单任务：{submission.request.title} [{submission.source_id}]', flush=True)
    for index, target in enumerate(targets, 1):
        check_cancel()
        print(f'查询清晰度 {index}/{len(targets)}：{submission.source_id} P{target.part}', flush=True)
        try:
            auth.ensure_login(context, settings)
            with transfer.make_downloader(context, settings) as downloader:
                raw = extract_target(downloader, target, context, settings)
                initial_sizes[target.part] = {}
                catalogs[target.part] = qualities.available_qualities(downloader, raw, size_info=initial_sizes[target.part])
                if use_merge:
                    variant_catalogs[target.part] = qualities.available_variants(downloader, raw, catalogs[target.part])
                    raw_defaults[target.part] = {}
                    for code, quality in catalogs[target.part].items():
                        info = qualities.select_quality(downloader, raw, quality)
                        formats = media.chosen_formats(info)
                        raw_defaults[target.part][code] = variants.StreamChoice(quality, tuple(
                            {key: fmt.get(key) for key in ('format_id', 'vcodec', 'acodec', 'width', 'height', 'fps')} for fmt in formats),
                            **media.expected_size(formats))
        except Exception as exc:
            catalogs.pop(target.part, None)
            submission.failures.append(f'{target.url} 清晰度查询：{exc}')
            mark_failure(settings, PartJob(submission, target, models.LEGACY_QUALITY), exc)
            if target.part in submission.part_activities:
                submission.part_activities[target.part].update('failed', detail=f'清晰度查询：{exc}')
    return SubmissionCatalog(catalogs, variant_catalogs, raw_defaults, initial_sizes, use_merge)


def group_submissions(submissions: list[Submission]) -> list[list[Submission]]:
    """按 submissions 的关联季标记归并画质组，重叠成员传递合并；无标记任务独立，返回按原输入顺序排列的各组。"""
    groups: list[list[Submission]] = []
    order = {id(submission): index for index, submission in enumerate(submissions)}
    for submission in submissions:
        members = submission.request.quality_group
        matching = [group for group in groups if members and any(members & item.request.quality_group for item in group)]
        if not matching:
            groups.append([submission])
            continue
        matching[0].append(submission)
        for group in matching[1:]:
            matching[0].extend(group)
            # 不使用 dataclass 相等比较，避免成员相互引用 jobs 后递归比较任务。
            groups = [item for item in groups if item is not group]
        matching[0].sort(key=lambda item: order[id(item)])
    return groups


def prepare_submission(settings: Settings, submission: Submission, catalog: SubmissionCatalog) -> None:
    """按 settings 用 catalog 及 submission 的逐集排名或原有上限分配实际画质、版本和大小；无返回值，不查询或交互。"""
    metadata, targets = submission.metadata, submission.targets
    record = library.snapshot_video(settings, submission.source_id)
    catalogs, variant_catalogs = catalog.catalogs, catalog.variant_catalogs
    raw_defaults, initial_sizes, use_merge = catalog.raw_defaults, catalog.initial_sizes, catalog.use_merge
    shared_rank = bool(submission.request.quality_group)
    selected = submission.selected_rank is not None if shared_rank else submission.ceiling is not None
    if selected:
        label = (f'画质档位：第 {submission.selected_rank} 高（各集分别排名）' if shared_rank
                 else f'画质上限：{submission.ceiling.name} (qn={submission.ceiling.code})')
        print(label, flush=True)
        if submission.activity:
            submission.activity.update(quality=label)
    for target in targets:
        if target.part not in catalogs:
            continue
        try:
            if not selected:
                raise ToolError('未分配清晰度：用户跳过或没有可下载档位')
            submission.assignments[target.part] = (ranked_quality(catalogs[target.part], submission.selected_rank) if shared_rank
                                                   else qualities.below_ceiling(catalogs[target.part], submission.ceiling))
        except ToolError as exc:
            submission.failures.append(f'{submission.source_id} P{target.part}：{exc}')
            if target.part in submission.part_activities:
                submission.part_activities[target.part].update('failed' if selected else 'skipped', detail=str(exc))
    if use_merge and selected and submission.assignments:
        # 拼接优先仍可降低实际档位；逐集排名时，各集只提供不高于其所选档位的兼容候选。
        compatible = ({part: {code: choices for code, choices in options.items()
                              if code <= submission.assignments[part].code}
                       for part, options in variant_catalogs.items() if part in submission.assignments}
                      if shared_rank else variant_catalogs)
        ceiling = max(submission.assignments.values(), key=lambda quality: quality.code) if shared_rank else submission.ceiling
        plan_groups(submission, compatible, {part: raw_defaults[part][quality.code]
                                            for part, quality in submission.assignments.items()}, ceiling)
    else:
        submission.jobs = [PartJob(submission, target, submission.assignments[target.part])
                           for target in targets if target.part in submission.assignments]
    print('下载版本：' + '；'.join(f'P{job.target.part} → {job.quality.name}'
                                + (' · ' + variants.label(list(job.choice.formats)) if job.choice else '')
                                for job in submission.jobs), flush=True)
    for plan in submission.merge_plans or []:
        print(f'拼接组 [{plan.expression}]：' + (plan.error or next(iter(plan.choices.values())).quality.name), flush=True)
    reused_parts = set()
    for job in submission.jobs:
        description = job.quality.name + (' · ' + variants.label(list(job.choice.formats)) if job.choice else '')
        if job.choice:
            job.actual_variant = job.choice.variant
            planned_formats = list(job.choice.formats)
            job.size_info.update(expected_size_bytes=job.choice.expected_size_bytes,
                                 expected_size_estimated=job.choice.expected_size_estimated)
        else:
            cached_size = initial_sizes.get(job.target.part, {}).get(job.quality.code, {})
            job.actual_variant = cached_size.get('format_variant', '')
            planned_formats = cached_size.get('formats', [])
            job.size_info.update({key: cached_size[key] for key in ('expected_size_bytes', 'expected_size_estimated')
                                 if key in cached_size})
        job.size_info['download_key'] = list(job_key(job))
        # 已完成文件仅在身份、内容版本和格式均一致时提供实际大小，不把其他画质或旧版本算入本次目标。
        for item in record.data['files']:
            if (item.get('status') == 'completed' and item.get('part') == job.target.part
                    and item.get('quality_id') == job.quality.code and item.get('source_cid') == job.target.cid
                    and item.get('episode_id') == job.target.episode_id and planned_formats
                    and item.get('formats') == planned_formats):
                try:
                    candidate = record.resolve_file(item)
                    size = media.size_number(candidate.stat().st_size) if candidate.is_file() else None
                except OSError:
                    size = None
                if size is not None:
                    job.size_info['actual_size_bytes'] = size
                    job.known_size_formats = planned_formats
                break
        print(f'[{submission.source_id} P{job.target.part} · {description}] 文件大小：'
              + media.file_size_fields(job.size_info)['file_size_text'], flush=True)
        if job.target.part not in reused_parts:
            job.activity = submission.part_activities.get(job.target.part)
            reused_parts.add(job.target.part)
        elif submission.activity:
            job.activity = submission.activity.journal.add('下载', metadata['title'], task=submission.log_task,
                                                          part=f'P{job.target.part}')
        if job.activity:
            job.activity.update(quality=description, source_part=job.target.part,
                                **job.size_info,
                                format_variant=job.choice.variant if job.choice else '',
                                groups=[plan.expression for plan in submission.merge_plans or []
                                        if job.target.part in plan.choices and job.choice
                                        and plan.choices[job.target.part].variant == job.choice.variant
                                        and plan.choices[job.target.part].quality.code == job.quality.code])
    register_pending(settings, submission)
    print(f'[{submission.source_id}] 目标视频总大小：' + media.summarize_sizes(size_entries([submission]))['text'], flush=True)


def finish_submission(settings: Settings, executable: str, submission: Submission, *, journal: HtmlLog | None = None) -> None:
    """按 settings/executable 收尾已就绪的 submission，无需等待其他投稿；无返回值。

    使用 submission 的实际画质及逐组来源方案收尾，不另行选择合并档位；journal 为可选 HTML 日志。
    工作线程只读取当前记录快照，不执行目录迁移或终端交互。
    """
    selection = submission.request.merge_parts.strip()
    check_cancel()
    record = library.snapshot_video(settings, submission.source_id)
    sources = {job.target.part: job.saved_id for job in submission.jobs if job.saved_id}
    try:
        if submission.merge_plans is not None:
            for plan in submission.merge_plans:
                missing = [part for part, choice in plan.choices.items() if not any(
                    job.target.part == part and job.choice and job.choice.variant == choice.variant
                    and job.quality.code == choice.quality.code and job.saved_id for job in submission.jobs)]
                if missing and not plan.error:
                    plan.error = f'本组所需来源版本未下载完成：{missing}'
            if merging.merge_plans(settings, executable, record, submission.merge_plans,
                                   full=selection == '0', journal=journal, task=submission.log_task):
                submission.failures.append(f'{submission.source_id}：分组拼接存在失败')
        elif selection not in ('', '-1', '0'):
            options = {'assignments': submission.assignments}
            if sources:
                options['source_entries'] = sources
            if journal:
                options.update(journal=journal, task=submission.log_task)
            if merging.merge_groups(settings, executable, record, selection, **options):
                submission.failures.append(f'{submission.source_id}：分组拼接存在失败')
        elif selection == '0' and submission.request.all_parts and len(submission.targets) > 1:
            if len(submission.downloaded) != len(submission.targets):
                submission.failures.append(f'{submission.source_id}：部分分P未下载完成，跳过自动拼接')
                merging.record_merge_result(record, status='skipped', source_parts=None, expression='全部分 P',
                                            assignments=submission.assignments, error='部分分 P 未下载完成，跳过自动拼接')
                if journal:
                    journal.merge_activity(submission.metadata['title'], task=submission.log_task, part='全部分 P').update(
                        'skipped', detail='部分分 P 未下载完成，跳过自动拼接')
            else:
                options = {}
                activity = journal.merge_activity(submission.metadata['title'], task=submission.log_task, part='全部分 P') if journal else None
                if activity:
                    options['activity'] = activity
                if sources:
                    options['source_entries'] = sources
                if submission.assignments:
                    choices = {quality.code: quality for quality in submission.assignments.values()}
                    if len(choices) != 1:
                        message = '本任务各分P使用不同画质，合并全部要求实际档位一致'
                        merging.record_merge_result(record, status='failed', source_parts=None, expression='全部分 P',
                                                    assignments=submission.assignments, error=message)
                        if activity:
                            activity.update('failed', detail=message)
                        raise ToolError(message)
                    options['quality'] = next(iter(choices.values()))
                merging.merge_parts(settings, executable, record, **options)
    except Exception as exc:
        submission.failures.append(f'{submission.source_id} 拼接：{exc}')
    current = library.snapshot_video(settings, submission.source_id)
    submission.failures.extend(f"{submission.source_id} 信息：{error}" for error in current.data['errors'])
    for item in current.data['files']:
        if item['id'] in submission.saved_ids:
            submission.failures.extend(f"{item['id']} 信息：{error}" for error in item['errors'])
    if submission.activity:
        submission.activity.update('warning' if submission.failures else 'completed',
                                   detail='\n'.join(submission.failures) if submission.failures else '下载及所需合并均已完成')


def download_submissions(context: Any, settings: Settings, executable: str, submissions: list[Submission],
                         *, journal: HtmlLog | None = None) -> list[str]:
    """用 context 主线程查询 submissions 并按关联季共用一次确认，按 settings 下载并以 executable 封装，返回失败摘要。

    journal 为可选 HTML 日志；投稿全部分 P 处理结束后，立即进入独立的串行合并队列。
    """
    prepared, failures = [], []
    catalogs: dict[int, SubmissionCatalog] = {}

    def preparation_failed(submission: Submission, error: Exception) -> None:
        """将 submission 的准备错误 error 追加到失败摘要并同步任务及尚未开始的分P日志；无返回值。"""
        failures.append(f'{submission.source_id} 准备下载：{error}')
        if submission.activity:
            submission.activity.update('failed', detail=str(error))
            for activity in submission.part_activities.values():
                if activity.entry['status'] == 'pending':
                    activity.update('failed', detail=f'任务准备失败：{error}')

    for index, submission in enumerate(submissions, 1):
        check_cancel()
        if journal:
            submission.log_task = f'任务 {index} · {submission.source_id}' + (f' · {submission.request.title}' if submission.request.title else '')
            submission.activity = journal.add('任务', submission.metadata['title'], task=submission.log_task, status='preparing')
            submission.part_activities = {
                target.part: journal.add('下载', submission.metadata['title'], task=submission.log_task, part=f'P{target.part}')
                for target in submission.targets}
            for target in submission.targets:
                submission.part_activities[target.part].update(download_key=[submission.source_id, target.part,
                    target.episode_id, target.cid, None, ''])
            selection = submission.request.merge_parts.strip()
            groups = ([f'第 {group_index} 组 [{group.strip()}]' for group_index, group in enumerate(selection.split(';'), 1)]
                      if selection not in ('', '-1', '0') else ['全部分 P']
                      if selection == '0' and submission.request.all_parts and len(submission.targets) > 1 else [])
            for group in groups:
                journal.add('合并', submission.metadata['title'], task=submission.log_task, part=group,
                            detail='等待所需分 P 就绪')
        try:
            catalogs[id(submission)] = query_submission(context, settings, submission)
        except Exception as exc:
            preparation_failed(submission, exc)
    for group in group_submissions(submissions):
        check_cancel()
        available = [submission for submission in group if id(submission) in catalogs]
        if not available:
            continue
        if available[0].request.quality_rank is None:
            shared = bool(available[0].request.quality_group)
            options = {(submission.source_id, part) if shared else part: choices
                       for submission in available for part, choices in catalogs[id(submission)].catalogs.items()}
            arguments = {'per_group': True} if any(catalogs[id(item)].use_merge for item in available) else {}
            try:
                selection = qualities.confirm_rank(options) if shared else qualities.confirm_ceiling(options, **arguments)
            except Exception as exc:
                for submission in available:
                    preparation_failed(submission, exc)
                continue
            for submission in available:
                if shared:
                    submission.selected_rank = selection
                else:
                    submission.ceiling = selection
        for submission in available:
            check_cancel()
            try:
                catalog = catalogs[id(submission)]
                if submission.request.quality_rank is not None:
                    union = {code: choice for choices in catalog.catalogs.values() for code, choice in choices.items()}
                    submission.ceiling = ranked_quality(union, submission.request.quality_rank)
                prepare_submission(settings, submission, catalog)
                prepared.append(submission)
                if submission.activity:
                    submission.activity.update('downloading', detail='已分配画质，等待分 P 下载完成')
            except Exception as exc:
                preparation_failed(submission, exc)
    order = {id(submission): index for index, submission in enumerate(submissions)}
    prepared.sort(key=lambda submission: order[id(submission)])
    print('本次运行目标视频总大小：' + media.summarize_sizes(size_entries(submissions))['text'], flush=True)
    controller = runtime.current()
    cancel_event = controller.event if controller else Event()
    active: dict[tuple, list[PartJob]] = {}
    completed: dict[tuple, PartResult | BaseException] = {}
    remaining = {id(submission): {id(job) for job in submission.jobs} for submission in prepared}
    finalized: set[int] = set()
    merger = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bilibili-merge')
    finishing: list[tuple[Submission, Future]] = []

    def finish_ready(submission: Submission) -> None:
        """若 submission 的分 P 均已结算，只派发一次使用实际档位的收尾任务；无返回值。"""
        identity = id(submission)
        if remaining[identity] or identity in finalized:
            return
        finalized.add(identity)
        if submission.activity:
            submission.activity.update('queued', detail='本任务分 P 已处理完，等待合并及收尾')
        check_cancel(cancel_event)
        finishing.append((submission, merger.submit(runtime.run_bound, controller, finish_submission, settings, executable, submission,
                                                    journal=journal)))

    def accept(job: PartJob, result: PartResult | BaseException) -> None:
        """将一次实际传输的 result 应用到 job 所属清单项；返回 None，不在工作线程调用。"""
        if isinstance(result, BaseException):
            try:
                mark_failure(settings, job, result)
            except (OSError, ToolError) as write_error:
                result.add_note(f'下载状态更新失败：{write_error}')
            job.submission.failures.append(f'{job.target.url} {job.quality.name}：{result}')
            activity = job.activity
            if activity:
                activity.update('interrupted' if isinstance(result, (KeyboardInterrupt, DownloadCancelled)) else 'failed',
                                detail=str(result) or '用户中止')
        else:
            job.size_info.update(result.size_info)
            job.saved_id = result.saved_id
            job.submission.downloaded.add(job.target.part)
            job.submission.saved_ids.add(result.saved_id)
            job.submission.failures.extend(result.errors)
            activity = job.activity
            if activity:
                status = 'reused' if activity.entry['status'] in ('pending', 'reused', 'preparing') else 'completed'
                activity.update('warning' if result.errors else status, progress=100,
                                **job.size_info,
                                path=result.path,
                                detail='\n'.join(result.errors) if result.errors else '视频及字幕处理完成')
        remaining[id(job.submission)].discard(id(job))
        finish_ready(job.submission)

    def prepare(job: PartJob) -> PartPayload | None:
        """在主线程按逐集排名或原上限刷新 job 的流和字幕、复制登录态；返回可执行负载，重复或失败任务返回 None。"""
        submission = job.submission
        check_cancel(cancel_event)
        try:
            auth.ensure_login(context, settings)
            with transfer.make_downloader(context, settings) as downloader:
                raw = extract_target(downloader, job.target, context, settings)
                if job.choice:
                    # 只刷新所规划流的地址，不能让后来出现的其他帧率替换仍可用的原版本。
                    raw['formats'] = [fmt for fmt in raw['formats'] if any(
                        all(fmt.get(key) == selected.get(key) for key in ('vcodec', 'acodec', 'width', 'height', 'fps'))
                        for selected in job.choice.formats)]
                    info = qualities.select_quality(downloader, raw, job.quality,
                                                    variants.compatibility(list(job.choice.formats)))
                    if variants.variant_id(media.chosen_formats(info)) != job.choice.variant:
                        raise ToolError('指定格式版本的帧率或编码已变化，请重新运行以规划拼接')
                    info['download_variant'] = job.choice.variant
                else:
                    options = qualities.available_qualities(downloader, raw)
                    choice = (ranked_quality(options, submission.selected_rank) if submission.request.quality_group
                              else qualities.below_ceiling(options, submission.ceiling))
                    if choice.code != job.quality.code:
                        mark_failure(settings, job, '平台档位变化，已按各集排名重新选档' if submission.request.quality_group
                                     else '平台档位变化，已在原上限内选择实际可用档位')
                    job.quality = choice
                    submission.assignments[job.target.part] = choice
                    info = qualities.select_quality(downloader, raw, job.quality)
                job.actual_variant = variants.variant_id(media.chosen_formats(info))
                refreshed_formats = [{key: fmt.get(key) for key in ('format_id', 'vcodec', 'acodec', 'width', 'height', 'fps')}
                                     for fmt in media.chosen_formats(info)]
                if (job.size_info.get('download_key') != list(job_key(job))
                        or (job.known_size_formats is not None and job.known_size_formats != refreshed_formats)):
                    job.size_info['actual_size_bytes'] = None
                job.size_info.update(media.expected_size(media.chosen_formats(info)), download_key=list(job_key(job)))
                if job.activity:
                    job.activity.update(**job.size_info)
                identity = job_key(job)
                if identity in completed:
                    accept(job, completed[identity])
                    return None
                if identity in active:
                    active[identity].append(job)
                    return None
                errors, subtitle_error = [], None
                try:
                    tracks = subtitles.list_subtitles(context, job.target, info, settings)
                except Exception as exc:
                    tracks, subtitle_error = [], str(exc)
                    errors.append(f'{job.target.url} 字幕列表：{exc}')
                print(f'[{submission.source_id} P{job.target.part}] 已选 {job.quality.name} · '
                      + media.file_size_fields(job.size_info)['file_size_text'], flush=True)
                if job.activity and not job.choice:
                    job.activity.update(quality=job.quality.name)
                snapshot = auth.snapshot_session(context)
                with library.video_transaction(settings, submission.source_id) as record:
                    record.entry(f'{submission.source_id}_p{job.target.part}', part=job.target.part, quality=job.quality,
                                 variant=job.choice.variant if job.choice else '')
                    record.save()
                active[identity] = [job]
                return PartPayload(job, info, tracks, snapshot, errors)
        except Exception as exc:
            accept(job, exc)
            return None

    def execute_part(payload: PartPayload, event: Event) -> PartResult:
        """在独立线程传输 payload，按 event 协作中止；每次建立独立会话及下载器，返回结果。"""
        check_cancel(event)
        job = payload.job
        activity = job.activity
        if activity:
            activity.update('downloading', detail='开始传输原始音视频流')
        print(f'[{job.submission.source_id} P{job.target.part}] 开始下载', flush=True)
        sizes = media.StreamSizes(media.chosen_formats(payload.info))
        size_announced = sizes.expected()['expected_size_bytes'] is not None
        with auth.worker_session(payload.snapshot, settings) as session, transfer.make_downloader(session, settings) as downloader:
            def progress(status: dict[str, Any]) -> None:
                """响应 status 并修正当前流完整大小；首次补齐大小时输出一次，按现有间隔写网页进度，无返回值。"""
                nonlocal size_announced
                check_cancel(event)
                if status.get('status') in {'downloading', 'finished'}:
                    sizes.progress(status)
                    job.size_info.update(sizes.expected())
                    if not size_announced and job.size_info['expected_size_bytes'] is not None:
                        size_announced = True
                        print(f'[{job.submission.source_id} P{job.target.part}] 文件大小：'
                              + media.file_size_fields(job.size_info)['file_size_text'], flush=True)
                    if activity:
                        activity.progress(status, size_info=sizes.expected())

            downloader.add_progress_hook(progress)
            if settings.threads > 1:
                downloader.params['noprogress'] = True
            options = {'activity': activity} if activity else {}
            folder, stem = transfer.save_video(downloader, payload.info, settings, executable,
                                                job.submission.metadata['title'], job.target.total_parts,
                                                source_cid=job.target.cid, prepared=True, cancel_event=event, size_tracker=sizes, **options)
            job.size_info.update(sizes.expected(), actual_size_bytes=payload.info.get('actual_size_bytes'))
            check_cancel(event)
            errors = subtitles.fetch_subtitles(session, payload.tracks, folder, stem, settings, cancel_event=event)
        print(f'[{job.submission.source_id} P{job.target.part}] 处理完成', flush=True)
        return PartResult(payload.info.get('saved_entry_id', models.quality_entry_id(str(payload.info['id']), job.quality)), [*payload.errors, *errors],
                          payload.info.get('saved_path', str(activity.entry['path']) if activity else ''), dict(job.size_info))

    def execute(payload: PartPayload, event: Event) -> PartResult:
        """在当前运行上下文中执行 payload，event 控制取消；返回分 P 结果。"""
        return runtime.run_bound(controller, execute_part, payload, event)

    def complete(job: PartJob, result: PartResult | None, error: BaseException | None) -> None:
        """在主线程将 job 的 result/error 分发给所有复用者；取消继续上抛，不触发交互。"""
        if isinstance(error, (KeyboardInterrupt, DownloadCancelled)):
            raise KeyboardInterrupt('用户取消下载')
        outcome = error if error is not None else result
        if outcome is None:
            raise ToolError('下载任务缺少结果')
        identity = job_key(job)
        completed[identity] = outcome
        for owner in active.pop(identity):
            accept(owner, outcome)

    try:
        for submission in prepared:
            finish_ready(submission)
        pending = [job for submission in prepared for job in submission.jobs]
        if pending:
            run_jobs(pending, settings.threads, prepare, execute, complete, cancel_event)
        for submission, future in finishing:
            try:
                future.result()
            except Exception as exc:
                submission.failures.append(f'{submission.source_id} 收尾：{exc}')
                if submission.activity:
                    submission.activity.update('failed', detail='\n'.join(submission.failures))
    except BaseException:
        cancel_event.set()
        if controller:
            controller.cancel()
        raise
    finally:
        merger.shutdown(wait=True, cancel_futures=True)
        for submission in prepared:
            print(f'[{submission.source_id}] 最终目标视频总大小：'
                  + media.summarize_sizes(size_entries([submission]))['text'], flush=True)
        print('本次运行最终目标视频总大小：' + media.summarize_sizes(size_entries(submissions))['text'], flush=True)
    for submission in prepared:
        failures.extend(submission.failures)
    return failures


def download_submission(context: Any, settings: Settings, executable: str, metadata: dict[str, Any],
                        targets: list[VideoTarget], args: argparse.Namespace) -> list[str]:
    """将 metadata/targets 单投稿接入同一线程调度；context/settings 控制会话，executable 封装，args 控制拼接，返回失败项。"""
    request = DownloadRequest(targets[0].url, '', args.all_parts or settings.all_parts, args.merge_parts, None, identity.source_id(metadata))
    return download_submissions(context, settings, executable, [Submission(metadata, targets, request)])
