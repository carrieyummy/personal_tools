"""协调命令行各模式和多个投稿，汇总查询、下载与拼接结果。"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import sys
from urllib.parse import urlsplit

from . import api
from . import auth
from . import config as configuration
from . import downloader as transfer
from . import library
from . import media
from . import manifest
from . import merge as merging
from . import subtitles
from . import workflow
from .errors import ToolError
from .activity_log import HtmlLog
from .config import Settings
from . import runtime
from .errors import check_cancel


def run(args: argparse.Namespace) -> int:
    """执行 args 指定的查询、清晰度确认下载或离线拼接；返回 0/2，取消交由 main 返回 130。"""
    selection = args.merge_parts.strip()
    if args.merge_video and selection == '-1':
        print("已跳过分 P 拼接。")
        return 0
    settings = configuration.load_settings(args.config.expanduser().resolve())
    label = '离线拼接' if args.merge_video else '下载视频'
    with runtime.Cancellation(settings):
        logging = HtmlLog(settings, label) if not (args.refresh_metadata or args.login_only or args.list_formats) else nullcontext(None)
        with logging as journal:
            if journal:
                print(f'实时下载日志：{journal.url}', flush=True)
            result = run_with_settings(args, settings, journal)
            check_cancel()
            return result


def run_with_settings(args: argparse.Namespace, settings: Settings, journal: HtmlLog | None) -> int:
    """按 args 和已校验 settings 执行模式，将下载及拼接写入可选 journal；返回 0/2。"""
    selection = args.merge_parts.strip()
    if args.refresh_metadata:
        return library.refresh_metadata(settings, args, media.resolve_ffmpeg(settings, args.config.resolve().parent))
    if args.merge_video:
        url = api.normalize_url(args.merge_video)
        match = api.VIDEO_PATH.fullmatch(urlsplit(url).path)
        if not match or not match['id'].startswith('BV'):
            raise ToolError("手动拼接请输入已下载视频的 BV 号或普通 BV 视频链接")
        executable = media.resolve_ffmpeg(settings, args.config.resolve().parent)
        record = library.get_video_info(settings, match['id'])
        if selection not in ('', '0'):
            return merging.merge_groups(settings, executable, record, args.merge_parts, journal=journal)
        activity = journal.add('合并', record.data['title'], task=record.data['bvid'], part='全部分 P',
                               status='merging') if journal else None
        merging.merge_parts(settings, executable, record, activity=activity)
        return 0
    urls = args.url or []
    if not args.login_only and not urls:
        urls = [input("请输入 B 站链接、BV/AV 号或 YAML 下载清单路径：").strip()]
    requests = manifest.expand_requests(urls, settings, args.all_parts, args.merge_parts)
    executable = '' if args.login_only or args.list_formats else media.resolve_ffmpeg(settings, args.config.resolve().parent)
    failures = []
    with auth.authenticated_session(settings, args.cookies.expanduser().resolve() if args.cookies else None) as context:
        account = auth.ensure_login(context, settings)
        vip = account.get('vipStatus', account.get('vip', {}).get('status', 0))
        print(f"登录有效：{account.get('uname', '当前账号')}；大会员：{'有效' if vip else '未开通或已到期'}。")
        if args.login_only:
            return 0
        submissions: dict[tuple, workflow.Submission] = {}
        processed: set[str] = set()
        for index, request in enumerate(requests):
            check_cancel()
            url = request.url
            try:
                targets = api.resolve_targets(context, url, settings, request.all_parts)
                if args.list_formats:
                    for target in targets:
                        if target.url in processed:
                            continue
                        processed.add(target.url)
                        try:
                            auth.ensure_login(context, settings)
                            with transfer.make_downloader(context, settings) as downloader:
                                info = downloader.extract_info(target.url, download=False)
                                subtitle_error = None
                                try:
                                    tracks = subtitles.list_subtitles(context, target, info, settings)
                                except Exception as exc:
                                    tracks, subtitle_error = [], str(exc)
                                    failures.append(f"{target.url} 字幕列表：{exc}")
                                workflow.print_video_info(downloader, info, tracks, subtitle_error)
                        except Exception as exc:
                            failures.append(f"{target.url} 画质查询：{exc}")
                    continue
                resolved_id = api.VIDEO_PATH.fullmatch(urlsplit(targets[0].url).path)['id']
                params = {'bvid': resolved_id} if resolved_id.startswith('BV') else {'aid': resolved_id[2:]}
                metadata = api.api_json(context, '/x/web-interface/view', settings, params)['data']
                if not isinstance(metadata.get('title'), str) or not metadata['title']:
                    raise ToolError("投稿标题为空，无法创建视频目录")
                identity = ('direct', metadata['bvid']) if request.quality_rank is None else ('manifest', index)
                if identity in submissions:
                    combined = {target.part: target for target in submissions[identity].targets}
                    combined.update((target.part, target) for target in targets)
                    submissions[identity].targets = list(combined.values())
                else:
                    submissions[identity] = workflow.Submission(metadata, targets, request)
            except Exception as exc:
                failures.append(f"{request.source}：{exc}")
                if journal:
                    journal.add('任务', request.title or request.url, task=request.source, status='failed', detail=str(exc))
        if not args.list_formats:
            failures.extend(workflow.download_submissions(context, settings, executable, list(submissions.values()), journal=journal))
    if failures:
        print("\n以下项目未完成；成功文件及下载进度已保留：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 2
    print("\n全部处理完成。")
    return 0
