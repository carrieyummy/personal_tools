"""创建认证下载器，按确认的清晰度保存视频并维护续传状态。"""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Any

from . import api
from . import auth
from . import library
from . import media
from . import models
from . import naming
from . import provenance
from . import variants
from .config import Settings
from .errors import DownloadCancelled, ToolError, check_cancel
from .models import Quality
from .activity_log import Activity
from . import runtime
from .storage import replace_file_transaction


def make_downloader(context: Any, settings: Settings) -> Any:
    """根据 settings 创建 yt-dlp，并从登录 context 注入内存 Cookie；返回需由调用者关闭的下载器。"""
    from yt_dlp import YoutubeDL

    if isinstance(context, auth.AuthenticatedHttpSession):
        user_agent = context.user_agent
    else:
        page = context.pages[0] if context.pages else context.new_page()
        user_agent = page.evaluate("navigator.userAgent")
    downloader = YoutubeDL({
        "format": settings.format_selector, "format_sort": settings.format_sort,
        "noplaylist": True, "retries": settings.retries, "fragment_retries": settings.fragment_retries,
        "socket_timeout": settings.socket_timeout, "continuedl": True, "overwrites": False,
        "skip_unavailable_fragments": False,
        "quiet": True, "noprogress": True,
        "proxy": settings.proxy,
        "fixup": "never", "postprocessors": [], "writesubtitles": False,
        "writeautomaticsub": False, "http_headers": {"User-Agent": user_agent, "Referer": api.HOME_URL},
    })
    auth.inject_cookies(downloader, context.cookies([api.HOME_URL, api.API_ROOT + "/"]))
    runtime.attach_downloader(downloader)
    if not any(cookie.name == "SESSDATA" and cookie.value for cookie in downloader.cookiejar):
        downloader.close()
        raise ToolError("登录检查通过但未取得 SESSDATA，请在专用 Chrome 中重新登录")
    return downloader


def save_video(downloader: Any, info: dict[str, Any], settings: Settings, executable: str,
               video_title: str, total_parts: int, *, source_cid: int, prepared: bool = False,
               cancel_event: Event | None = None, activity: Activity | None = None) -> tuple[Path, str]:
    """保存 info 选中的原始流；settings/executable 控制路径和封装，video_title/total_parts 描述投稿。

    source_cid 为本次查询的正整数内容 ID；只有版本和格式一致才复用，未知旧版本需要显式建立基线。
    prepared 表示主线程已经整理记录，工作线程只读写当前布局；cancel_event 请求中止并保留当前版本断点。
    activity 为可选 HTML 日志条目，记录复用、封装及最终文件位置。
    返回媒体目录和文件前缀，记录读改写按 BV 加锁，下载、封装和媒体探测均在锁外执行。
    """
    check_cancel(cancel_event)
    if type(source_cid) is not int or source_cid < 1:
        raise ToolError('下载来源 CID 必须为正整数')
    formats = media.chosen_formats(info)
    video_id = naming.safe_name(str(info['id']), 60)
    bvid = video_id.split('_p')[0]
    part = int(video_id.rsplit('_p', 1)[1])
    selected = info.get('selected_quality')
    quality = Quality(selected['code'], selected['name']) if selected else models.entry_quality({'formats': formats})
    signature = [{key: fmt.get(key) for key in ('format_id', 'vcodec', 'acodec', 'width', 'height', 'fps')} for fmt in formats]
    variant = info.get('download_variant', '')
    existing = None
    with library.video_transaction(settings, bvid, video_title, total_parts, prepare=not prepared) as record:
        record.data['total_parts'] = total_parts
        if not variant:
            # 拼接优先已保存的相同版本也可供后续清晰度优先任务复用。
            known = variants.variant_id(signature)
            if any(item.get('part') == part and item.get('quality_id') == quality.code and item.get('formats')
                   and variants.variant_id(item['formats']) == known for item in record.data['files']):
                variant = known
        item = record.entry(video_id, part=part, title=info['title'], quality=quality, variant=variant)
        if variant:
            # 逐P计划的空占位不应与已保存的兼容版本形成第二份相同来源记录。
            previous_count = len(record.data['files'])
            record.data['files'] = [other for other in record.data['files'] if other is item
                                    or other['id'] != models.quality_entry_id(video_id, quality)
                                    or other.get('path') or other.get('formats')]
            if len(record.data['files']) != previous_count:
                record.save()
        info['saved_entry_id'] = item['id']
        folder = record.quality_directory(quality)
        folder.mkdir(parents=True, exist_ok=True)
        base_stem = naming.video_stem(settings, info['title'], video_id)
        if ':fmt' in item['id']:
            base_stem += '_fmt' + variant[:12]
        stem = naming.quality_stem(base_stem, record.quality_suffix(quality), record.mix_quality)
        if item['status'] == 'completed' and item.get('source_cid') == source_cid and item['formats'] == signature:
            candidate = record.resolve_file(item)
            if candidate.is_file() and candidate.stat().st_size:
                existing, stem = candidate, str(item['stem'])
        if existing is None and item['status'] != 'completed':
            item.update(title=info['title'], stem=stem, base_stem=base_stem, mix_quality=record.mix_quality, formats=signature,
                        format_variant=variants.variant_id(signature), processing='stream copy',
                        status='downloading', errors=[])
            record.save()
    if existing is not None:
        library.complete_video(settings, bvid, video_id, quality, existing, executable, source_cid=source_cid, variant=variant, part=part)
        if activity:
            activity.update('reused', path=str(existing), detail='来源 CID、本地文件及格式一致，跳过重复传输')
        print(f'视频已完成，跳过下载：{existing}')
        return existing.parent, stem
    staging_root = folder / '.streams' / video_id
    if record.mix_quality:
        staging_root /= f'qn{quality.code}' if quality.code is not None else 'legacy'
    staging = staging_root / provenance.stream_version(source_cid, signature)
    staging.mkdir(parents=True, exist_ok=True)
    sources = []
    try:
        for index, fmt in enumerate(formats):
            check_cancel(cancel_event)
            extension = naming.safe_name(str(fmt.get('ext', 'bin')), 10)
            format_id = naming.safe_name(str(fmt.get('format_id', index)), 40)
            source = staging / f'{index}.{format_id}.{extension}'
            stream_info = {**fmt, 'id': info['id'], 'title': info['title'],
                           'http_headers': {**info.get('http_headers', {}), **fmt.get('http_headers', {})}}
            # dl 只执行传输，不运行 yt-dlp 的自动合并、修复或转码后处理。
            if activity:
                partial = source.with_name(source.name + '.part')
                resume_source = partial if partial.exists() else source
                activity.start_transfer(('视频' if fmt.get('vcodec') != 'none' else '音频') + '流 · ' + format_id,
                                        resume_source.stat().st_size if resume_source.exists() else 0)
            try:
                success, _ = downloader.dl(str(source), stream_info)
            finally:
                if activity:
                    activity.end_transfer()
            check_cancel(cancel_event)
            if not success or not source.is_file() or not source.stat().st_size:
                raise ToolError(f'原始流下载未完成：{source.name}；重新运行可续传')
            sources.append(source)
        check_cancel(cancel_event)
        if activity:
            activity.update('processing', progress=None, detail='原始流传输完成，正在无损封装')
        output = media.mux_streams(executable, sources, formats, staging, stem)
        fingerprint = provenance.file_fingerprint(output, cancel_event=cancel_event)
        destination = folder / output.name
        check_cancel(cancel_event)
        with library.video_transaction(settings, bvid) as current:
            expected = {page['part']: page['cid'] for page in current.data.get('current_parts', [])}
            if expected and expected.get(part) != source_cid:
                raise ToolError(f'第 {part} P 来源在下载期间变化，请重新运行')
            item = current.entry(video_id, part=part, quality=quality, variant=variant)
            if any(other['id'] != item['id'] and other.get('path') and current.resolve_file(other) == destination
                   for other in current.data['files']):
                raise ToolError('格式版本文件名发生冲突，保留原文件')
            if (':fmt' in item['id'] and destination.exists()
                    and (not item.get('path') or current.resolve_file(item) != destination)):
                raise ToolError('格式版本目标已存在但未登记，保留原文件')

            def commit() -> None:
                """成功替换媒体后提交当前版本、格式和内容指纹；失败由文件事务回滚，无参数及返回值。"""
                check_cancel(cancel_event)
                item.update(title=info['title'], stem=stem, base_stem=base_stem, mix_quality=current.mix_quality, formats=signature,
                            format_variant=variants.variant_id(signature), processing='stream copy',
                            source_cid=source_cid, content_fingerprint=fingerprint, errors=[])
                current.record_file(item, destination)
                current.save()

            replace_file_transaction(current.output_dir, output, destination, current.index_paths, commit)
        library.complete_video(settings, bvid, video_id, quality, destination, executable, source_cid=source_cid, variant=variant, part=part)
        if activity:
            activity.update(path=str(destination), detail='视频已保存，正在处理字幕')
    except (Exception, KeyboardInterrupt) as exc:
        try:
            with library.video_transaction(settings, bvid) as current:
                item = current.entry(video_id, part=part, quality=quality, variant=variant)
                if item['status'] != 'completed':
                    interrupted = isinstance(exc, (KeyboardInterrupt, DownloadCancelled)) or (cancel_event is not None and cancel_event.is_set())
                    item.update(status='interrupted' if interrupted else 'failed', errors=[str(exc) or '用户中止'])
                    current.save()
        except (OSError, ToolError) as write_error:
            exc.add_note(f'下载状态保存失败：{write_error}')
        raise
    for source in sources:
        source.unlink(missing_ok=True)
    cleanup = [staging, staging.parent, staging.parent.parent]
    if record.mix_quality:
        cleanup.append(staging.parent.parent.parent)
    for directory in cleanup:
        try:
            directory.rmdir()
        except OSError:
            # 其他分P可能刚创建断点目录，非空或已被清理均不影响本分P完成。
            pass
    print(f'视频已保存（原编码，无重新编码）：{destination}')
    return folder, stem
