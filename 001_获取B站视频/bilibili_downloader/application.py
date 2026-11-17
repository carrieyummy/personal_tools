"""协调命令行各模式和多个投稿，汇总查询、下载与拼接结果。"""

from __future__ import annotations

import argparse
from collections import deque
from contextlib import nullcontext
from dataclasses import replace
import sys
from urllib.parse import urlsplit

from . import api
from . import bangumi
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


def expand_season_request(context: object, request: manifest.DownloadRequest, settings: Settings) -> list[manifest.DownloadRequest]:
    """经 context/settings 展开 request 的关联季，普通链接标记共享画质组，保留清单排名及拼接设置；返回请求列表。"""
    url = api.resolve_url(context, request.url, settings)
    if not api.BANGUMI_PATH.fullmatch(urlsplit(url).path):
        return [replace(request, url=url)]
    season_urls = bangumi.related_season_urls(context, url, settings)
    group = (frozenset(api.BANGUMI_PATH.fullmatch(urlsplit(value).path)['id'] for value in season_urls)
             if request.quality_rank is None else frozenset())
    return [replace(request, url=season_url, all_parts=True, source=f'{request.source} · {season_url}', quality_group=group)
            for season_url in season_urls]


def run_with_settings(args: argparse.Namespace, settings: Settings, journal: HtmlLog | None) -> int:
    """按 args/settings 执行模式；all_seasons 逐季归档且普通链接按关联季共用画质确认，失败记录到 journal，返回 0/2。"""
    selection = args.merge_parts.strip()
    if args.refresh_metadata:
        return library.refresh_metadata(settings, args, media.resolve_ffmpeg(settings, args.config.resolve().parent))
    if args.merge_video:
        url = api.normalize_url(args.merge_video)
        match = api.VIDEO_PATH.fullmatch(urlsplit(url).path)
        season = api.BANGUMI_PATH.fullmatch(urlsplit(url).path)
        if match and match['id'].startswith('BV'):
            identifier = match['id']
        elif season and season['id'].startswith('ss'):
            identifier = season['id']
        else:
            raise ToolError("手动拼接请输入已下载视频的 BV 号、番剧 ss 号或对应链接")
        executable = media.resolve_ffmpeg(settings, args.config.resolve().parent)
        record = library.get_video_info(settings, identifier)
        if selection not in ('', '0'):
            return merging.merge_groups(settings, executable, record, args.merge_parts, journal=journal)
        activity = journal.add('合并', record.data['title'], task=record.data['source_id'], part='全部分 P',
                               status='merging') if journal else None
        merging.merge_parts(settings, executable, record, activity=activity)
        return 0
    urls = args.url or []
    if not args.login_only and not urls:
        urls = [input("请输入 B 站视频/番剧链接、BV/AV 或 ep/ss 号、YAML 下载清单路径：").strip()]
    requests = manifest.expand_requests(urls, settings, args.all_parts, args.merge_parts)
    executable = '' if args.login_only or args.list_formats else media.resolve_ffmpeg(settings, args.config.resolve().parent)
    failures = []
    with auth.authenticated_session(settings, args.cookies.expanduser().resolve() if args.cookies else None) as context:
        account = auth.ensure_login(context, settings)
        vip = account.get('vipStatus', account.get('vip', {}).get('status', 0))
        print(f"登录有效：{account.get('uname', '当前账号')}；大会员：{'有效' if vip else '未开通或已到期'}。")
        if args.login_only:
            return 0
        if not args.list_formats:
            from .discovery import submission_groups
            snapshot = auth.snapshot_session(context)

            def discovery_failed(request, error):
                """记录 request 的目录 error；不改变其他确认组，日志只保存业务说明。"""
                failures.append(f"{request.source}：{error}")
                if journal:
                    journal.add('任务', request.title or request.url, task=request.source,
                                status='failed', detail=str(error))

            groups = submission_groups(snapshot, requests, settings, discovery_failed)
            failures.extend(workflow.download_submissions(context, settings, executable, [],
                                                          journal=journal, submission_groups=groups))
        else:
            processed: set[str] = set()
            pending = deque((request, settings.all_seasons) for request in requests)
            index = 0
            while pending:
                check_cancel()
                request, expand_seasons = pending.popleft()
                index += 1
                url = request.url
                try:
                    if expand_seasons:
                        expanded = expand_season_request(context, request, settings)
                        pending.extendleft((item, False) for item in reversed(expanded))
                        continue
                    targets = api.resolve_targets(context, url, settings, request.all_parts)
                    for target in targets:
                        if target.url in processed:
                            continue
                        processed.add(target.url)
                        try:
                            auth.ensure_login(context, settings)
                            with transfer.make_downloader(context, settings) as downloader:
                                if target.episode_id is not None:
                                    info = workflow.extract_target(downloader, target, context, settings)
                                    info = downloader.process_ie_result(info, download=False)
                                else:
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
                except Exception as exc:
                    failures.append(f"{request.source}：{exc}")
                    if journal:
                        journal.add('任务', request.title or request.url, task=request.source, status='failed', detail=str(exc))
    if failures:
        print("\n以下项目未完成；成功文件及下载进度已保留：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 2
    print("\n全部处理完成。")
    return 0
