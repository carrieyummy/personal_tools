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


def extract_target(downloader: Any, target: VideoTarget) -> dict[str, Any]:
    """用 downloader 查询 target 的全部原始格式，不下载或自动选择最高档；返回单分 P 信息。"""
    info = downloader.extract_info(target.url, download=False, process=False)
    if not info or info.get("_type", "video") != "video":
        raise ToolError("链接未解析为单个普通投稿分 P")
    return info


@dataclass
class Submission:
    """投稿任务：metadata/targets/request 为原站信息、目标P及输入；ceiling 固定上限，jobs/merge_plans 保存版本及组方案。"""

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

    @property
    def bvid(self) -> str:
        """返回当前投稿的原站 BV 号，无参数。"""
        return self.metadata['bvid']


@dataclass
class PartJob:
    """描述下载版本：submission/target 为所属任务及P，quality 为画质，choice 锁定格式，activity 提供独立日志。"""

    submission: Submission
    target: VideoTarget
    quality: Quality
    choice: variants.StreamChoice | None = None
    activity: Activity | None = None
    actual_variant: str = ''
    saved_id: str = ''


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
    """分P成功传输后的结果：saved_id 为记录标识，errors 为失败摘要，path 为可选成品路径供复用日志使用。"""

    saved_id: str
    errors: list[str]
    path: str = ''


def ranked_quality(options: dict[int, Quality], rank: int) -> Quality:
    """从 options 按平台画质编号降序取正整数排名 rank；返回 Quality，排名不存在时报错。"""
    ordered = sorted(options.values(), key=lambda quality: quality.code, reverse=True)
    if rank > len(ordered):
        raise ToolError(f'画质排名 {rank} 不存在，实际可下载档位数为 {len(ordered)}')
    return ordered[rank - 1]


def mark_failure(settings: Settings, job: PartJob, error: BaseException | str) -> None:
    """按 settings 为 job 的未完成记录标记 error；取消标为 interrupted，已完成文件保持完成，无返回值。"""
    with library.video_transaction(settings, job.submission.bvid) as record:
        item = record.entry(f'{job.submission.bvid}_p{job.target.part}', part=job.target.part, quality=job.quality,
                            variant=job.choice.variant if job.choice else '')
        if item['status'] != 'completed':
            item.update(status='interrupted' if isinstance(error, (KeyboardInterrupt, DownloadCancelled)) else 'failed',
                        errors=[str(error) or '用户中止'])
            record.save()


def register_pending(settings: Settings, submission: Submission) -> None:
    """按 settings 为 submission 的全部下载版本登记 pending 条目和格式；保留已有状态，无返回值。"""
    with library.video_transaction(settings, submission.bvid) as record:
        for job in submission.jobs:
            item = record.entry(f'{submission.bvid}_p{job.target.part}', part=job.target.part, quality=job.quality,
                                variant=job.choice.variant if job.choice else '')
            if job.choice and not item['formats']:
                item.update(formats=list(job.choice.formats), format_variant=job.choice.variant)
        record.save()


def plan_groups(submission: Submission, catalogs: dict[int, dict[int, dict[tuple, variants.StreamChoice]]],
                defaults: dict[int, variants.StreamChoice]) -> None:
    """为 submission 逐组建立兼容计划及去重下载版本；catalogs/defaults 是兼容候选及逐P最高档，返回 None。"""
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
            plan.choices = qualities.common_choice(catalogs, plan.parts, submission.ceiling)
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


def prepare_submission(context: Any, settings: Settings, submission: Submission) -> None:
    """在主线程通过 context 准备 submission 的归档及画质；settings 控制目录和请求，无返回值。"""
    metadata, targets = submission.metadata, submission.targets
    record = library.get_video_info(settings, submission.bvid, metadata['title'], targets[0].total_parts,
                                   shared_folder=library.prepare_collection(settings, metadata))
    library.update_source_info(record, metadata)
    record.save()
    catalogs, variant_catalogs, raw_defaults = {}, {}, {}
    selection = submission.request.merge_parts.strip()
    use_merge = settings.priority == 'merge' and (selection not in ('', '-1', '0')
                 or (selection == '0' and submission.request.all_parts and len(targets) > 1))
    if submission.request.title:
        print(f'\n清单任务：{submission.request.title} [{submission.bvid}]', flush=True)
    for index, target in enumerate(targets, 1):
        check_cancel()
        print(f'查询清晰度 {index}/{len(targets)}：{submission.bvid} P{target.part}', flush=True)
        try:
            auth.ensure_login(context, settings)
            with transfer.make_downloader(context, settings) as downloader:
                raw = extract_target(downloader, target)
                catalogs[target.part] = qualities.available_qualities(downloader, raw)
                if use_merge:
                    variant_catalogs[target.part] = qualities.available_variants(downloader, raw, catalogs[target.part])
                    raw_defaults[target.part] = {}
                    for code, quality in catalogs[target.part].items():
                        info = qualities.select_quality(downloader, raw, quality)
                        formats = media.chosen_formats(info)
                        raw_defaults[target.part][code] = variants.StreamChoice(quality, tuple(
                            {key: fmt.get(key) for key in ('format_id', 'vcodec', 'acodec', 'width', 'height', 'fps')} for fmt in formats))
        except Exception as exc:
            catalogs.pop(target.part, None)
            submission.failures.append(f'{target.url} 清晰度查询：{exc}')
            mark_failure(settings, PartJob(submission, target, models.LEGACY_QUALITY), exc)
            if target.part in submission.part_activities:
                submission.part_activities[target.part].update('failed', detail=f'清晰度查询：{exc}')
    union = {code: choice for options in catalogs.values() for code, choice in options.items()}
    submission.ceiling = ((qualities.confirm_ceiling(catalogs, per_group=True) if use_merge else qualities.confirm_ceiling(catalogs))
                          if submission.request.quality_rank is None
                          else ranked_quality(union, submission.request.quality_rank))
    if submission.ceiling is not None:
        label = f'画质上限：{submission.ceiling.name} (qn={submission.ceiling.code})'
        print(label, flush=True)
        if submission.activity:
            submission.activity.update(quality=label)
    for target in targets:
        if target.part not in catalogs:
            continue
        try:
            if submission.ceiling is None:
                raise ToolError('未分配清晰度：用户跳过或没有可下载档位')
            submission.assignments[target.part] = qualities.below_ceiling(catalogs[target.part], submission.ceiling)
        except ToolError as exc:
            submission.failures.append(f'{submission.bvid} P{target.part}：{exc}')
            if target.part in submission.part_activities:
                submission.part_activities[target.part].update('skipped' if submission.ceiling is None else 'failed', detail=str(exc))
    if use_merge and submission.ceiling is not None:
        plan_groups(submission, variant_catalogs, {part: raw_defaults[part][quality.code]
                                                 for part, quality in submission.assignments.items()})
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
        if job.target.part not in reused_parts:
            job.activity = submission.part_activities.get(job.target.part)
            reused_parts.add(job.target.part)
        elif submission.activity:
            job.activity = submission.activity.journal.add('下载', metadata['title'], task=submission.log_task,
                                                          part=f'P{job.target.part}')
        if job.activity:
            job.activity.update(quality=description, source_part=job.target.part,
                                format_variant=job.choice.variant if job.choice else '',
                                groups=[plan.expression for plan in submission.merge_plans or []
                                        if job.target.part in plan.choices and job.choice
                                        and plan.choices[job.target.part].variant == job.choice.variant
                                        and plan.choices[job.target.part].quality.code == job.quality.code])
    register_pending(settings, submission)


def finish_submission(settings: Settings, executable: str, submission: Submission, *, journal: HtmlLog | None = None) -> None:
    """按 settings/executable 收尾已就绪的 submission，无需等待其他投稿；无返回值。

    使用 submission 的实际画质及逐组来源方案收尾，不另行选择合并档位；journal 为可选 HTML 日志。
    工作线程只读取当前记录快照，不执行目录迁移或终端交互。
    """
    selection = submission.request.merge_parts.strip()
    check_cancel()
    record = library.snapshot_video(settings, submission.bvid)
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
                submission.failures.append(f'{submission.bvid}：分组拼接存在失败')
        elif selection not in ('', '-1', '0'):
            options = {'assignments': submission.assignments}
            if sources:
                options['source_entries'] = sources
            if journal:
                options.update(journal=journal, task=submission.log_task)
            if merging.merge_groups(settings, executable, record, selection, **options):
                submission.failures.append(f'{submission.bvid}：分组拼接存在失败')
        elif selection == '0' and submission.request.all_parts and len(submission.targets) > 1:
            if len(submission.downloaded) != len(submission.targets):
                submission.failures.append(f'{submission.bvid}：部分分P未下载完成，跳过自动拼接')
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
        submission.failures.append(f'{submission.bvid} 拼接：{exc}')
    current = library.snapshot_video(settings, submission.bvid)
    submission.failures.extend(f"{submission.bvid} 信息：{error}" for error in current.data['errors'])
    for item in current.data['files']:
        if item['id'] in submission.saved_ids:
            submission.failures.extend(f"{item['id']} 信息：{error}" for error in item['errors'])
    if submission.activity:
        submission.activity.update('warning' if submission.failures else 'completed',
                                   detail='\n'.join(submission.failures) if submission.failures else '下载及所需合并均已完成')


def download_submissions(context: Any, settings: Settings, executable: str, submissions: list[Submission],
                         *, journal: HtmlLog | None = None) -> list[str]:
    """用 context 主线程准备 submissions，按 settings 下载并以 executable 封装，返回失败摘要。

    journal 为可选 HTML 日志；投稿全部分 P 处理结束后，立即进入独立的串行合并队列。
    """
    prepared, failures = [], []
    for index, submission in enumerate(submissions, 1):
        check_cancel()
        if journal:
            submission.log_task = f'任务 {index} · {submission.bvid}' + (f' · {submission.request.title}' if submission.request.title else '')
            submission.activity = journal.add('任务', submission.metadata['title'], task=submission.log_task, status='preparing')
            submission.part_activities = {
                target.part: journal.add('下载', submission.metadata['title'], task=submission.log_task, part=f'P{target.part}')
                for target in submission.targets}
            selection = submission.request.merge_parts.strip()
            groups = ([f'第 {group_index} 组 [{group.strip()}]' for group_index, group in enumerate(selection.split(';'), 1)]
                      if selection not in ('', '-1', '0') else ['全部分 P']
                      if selection == '0' and submission.request.all_parts and len(submission.targets) > 1 else [])
            for group in groups:
                journal.add('合并', submission.metadata['title'], task=submission.log_task, part=group,
                            detail='等待所需分 P 就绪')
        try:
            prepare_submission(context, settings, submission)
            prepared.append(submission)
            if submission.activity:
                submission.activity.update('downloading', detail='已分配画质，等待分 P 下载完成')
        except Exception as exc:
            failures.append(f'{submission.bvid} 准备下载：{exc}')
            if submission.activity:
                submission.activity.update('failed', detail=str(exc))
                for activity in submission.part_activities.values():
                    if activity.entry['status'] == 'pending':
                        activity.update('failed', detail=f'任务准备失败：{exc}')
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

    def key(job: PartJob) -> tuple:
        """返回 job 的 BV、分P、CID、实际画质和格式版本，用于准确传输去重。"""
        return job.submission.bvid, job.target.part, job.target.cid, job.quality.code, job.actual_variant

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
            job.saved_id = result.saved_id
            job.submission.downloaded.add(job.target.part)
            job.submission.saved_ids.add(result.saved_id)
            job.submission.failures.extend(result.errors)
            activity = job.activity
            if activity:
                status = 'reused' if activity.entry['status'] in ('pending', 'reused', 'preparing') else 'completed'
                activity.update('warning' if result.errors else status, progress=100,
                                path=result.path,
                                detail='\n'.join(result.errors) if result.errors else '视频及字幕处理完成')
        remaining[id(job.submission)].discard(id(job))
        finish_ready(job.submission)

    def prepare(job: PartJob) -> PartPayload | None:
        """在主线程按原上限刷新 job 的流和字幕、复制登录态；返回可执行负载，重复或失败任务返回 None。"""
        submission = job.submission
        check_cancel(cancel_event)
        try:
            auth.ensure_login(context, settings)
            with transfer.make_downloader(context, settings) as downloader:
                raw = extract_target(downloader, job.target)
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
                    choice = qualities.below_ceiling(qualities.available_qualities(downloader, raw), submission.ceiling)
                    if choice.code != job.quality.code:
                        mark_failure(settings, job, '平台档位变化，已在原上限内选择实际可用档位')
                    job.quality = choice
                    submission.assignments[job.target.part] = choice
                    info = qualities.select_quality(downloader, raw, job.quality)
                job.actual_variant = variants.variant_id(media.chosen_formats(info))
                identity = key(job)
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
                print(f'[{submission.bvid} P{job.target.part}] 已选 {job.quality.name}', flush=True)
                if job.activity and not job.choice:
                    job.activity.update(quality=job.quality.name)
                snapshot = auth.snapshot_session(context)
                with library.video_transaction(settings, submission.bvid) as record:
                    record.entry(f'{submission.bvid}_p{job.target.part}', part=job.target.part, quality=job.quality,
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
        print(f'[{job.submission.bvid} P{job.target.part}] 开始下载', flush=True)
        with auth.worker_session(payload.snapshot, settings) as session, transfer.make_downloader(session, settings) as downloader:
            def progress(status: dict[str, Any]) -> None:
                """响应下载 status 进度并检查共享取消标志；取消时抛出异常，正常无返回值。"""
                check_cancel(event)
                if activity and status.get('status') in {'downloading', 'finished'}:
                    activity.progress(status)

            downloader.add_progress_hook(progress)
            if settings.threads > 1:
                downloader.params['noprogress'] = True
            options = {'activity': activity} if activity else {}
            folder, stem = transfer.save_video(downloader, payload.info, settings, executable,
                                                job.submission.metadata['title'], job.target.total_parts,
                                                source_cid=job.target.cid, prepared=True, cancel_event=event, **options)
            check_cancel(event)
            errors = subtitles.fetch_subtitles(session, payload.tracks, folder, stem, settings, cancel_event=event)
        print(f'[{job.submission.bvid} P{job.target.part}] 处理完成', flush=True)
        return PartResult(payload.info.get('saved_entry_id', models.quality_entry_id(str(payload.info['id']), job.quality)), [*payload.errors, *errors],
                          str(activity.entry['path']) if activity else '')

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
        identity = key(job)
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
                submission.failures.append(f'{submission.bvid} 收尾：{exc}')
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
        failures.extend(submission.failures)
    return failures


def download_submission(context: Any, settings: Settings, executable: str, metadata: dict[str, Any],
                        targets: list[VideoTarget], args: argparse.Namespace) -> list[str]:
    """将 metadata/targets 单投稿接入同一线程调度；context/settings 控制会话，executable 封装，args 控制拼接，返回失败项。"""
    request = DownloadRequest(targets[0].url, '', args.all_parts or settings.all_parts, args.merge_parts, None, metadata['bvid'])
    return download_submissions(context, settings, executable, [Submission(metadata, targets, request)])
