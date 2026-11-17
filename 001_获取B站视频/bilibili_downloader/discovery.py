"""按完整画质确认组发现投稿；使用独立 HTTP 快照，不跨线程操作浏览器对象。"""

from dataclasses import replace
from urllib.parse import urlsplit

from . import api, auth, bangumi, identity
from .errors import ToolError, check_cancel, DownloadCancelled
from .workflow import Submission


def matches(request, submission) -> bool:
    """判断普通交互 request 是否属于 submission 的已验证来源；只比较来源编号和目录 EP，不发请求。"""
    if request.quality_rank is not None:
        return False
    path = urlsplit(request.url).path
    video, season = api.VIDEO_PATH.fullmatch(path), api.BANGUMI_PATH.fullmatch(path)
    metadata = submission.metadata
    if video:
        return video['id'] in {metadata.get('bvid'), 'av'+str(metadata.get('aid'))}
    if season:
        key = season['id']
        return (key == submission.source_id or key in submission.request.quality_group
                or key in {'ep'+str(page.get('episode_id')) for page in metadata.get('pages', [])})
    return False


def submission_groups(snapshot, requests: list, settings, failed):
    """用 snapshot/settings 按 requests 顺序返回完整关联确认组；failed(request,error) 记录局部失败。

    每次只校验当前组的目录，相关的后续普通链接合并后再确认画质；清单项保留独立排名。
    短链先解析真实编号；普通关联季先读取用于合并确认组的关系索引，逐集画质不提前查询。
    """
    pending = list(requests)
    related = {}
    with auth.worker_session(snapshot, settings) as session:
        for index, request in enumerate(pending):
            if urlsplit(request.url).hostname == 'b23.tv':
                check_cancel()
                try:
                    pending[index] = replace(request, url=api.resolve_url(session, request.url, settings))
                except DownloadCancelled:
                    raise
                except Exception as error:
                    failed(request, error)
                    pending[index] = None
        # 共享交互确认需要先识别关系闭包；接口返回不对称时也不能把重叠组问两次。
        # 这里只缓存关系索引，独立来源的完整选档和归档仍由后续组按需执行。
        if settings.all_seasons:
            for index, request in enumerate(pending):
                if request is None or request.quality_rank is not None or not api.BANGUMI_PATH.fullmatch(urlsplit(request.url).path):
                    continue
                check_cancel()
                try:
                    if request.url not in related:
                        related[request.url] = bangumi.related_season_urls(session, request.url, settings)
                except DownloadCancelled:
                    raise
                except Exception as error:
                    failed(request, error)
                    pending[index] = None
    while pending:
        check_cancel()
        request = pending.pop(0)
        if request is None:
            continue
        group, direct = [], {}

        def expand(session, request):
            """在独立 session 中展开 request；完整关联季作为一组，重复普通来源在组内合并。"""
            variants = [request]
            url = api.resolve_url(session, request.url, settings)
            if settings.all_seasons and api.BANGUMI_PATH.fullmatch(urlsplit(url).path):
                urls = related[url] if url in related else bangumi.related_season_urls(session, url, settings)
                members = (frozenset(api.BANGUMI_PATH.fullmatch(urlsplit(value).path)['id'] for value in urls)
                           if request.quality_rank is None else frozenset())
                variants = [replace(request, url=value, all_parts=True, quality_group=members,
                                    source=f'{request.source} · {value}') for value in urls]
            for variant in variants:
                check_cancel()
                try:
                    targets = api.resolve_targets(session, variant.url, settings, variant.all_parts)
                    metadata = api.target_metadata(session, targets[0], settings)
                    if not isinstance(metadata.get('title'), str) or not metadata['title']:
                        raise ToolError('投稿标题为空，无法创建视频目录')
                    source_id = identity.source_id(metadata)
                    if variant.quality_rank is None and source_id in direct:
                        existing = direct[source_id]
                        combined = {target.part:target for target in existing.targets}
                        combined.update((target.part, target) for target in targets)
                        existing.targets = sorted(combined.values(), key=lambda target:target.part)
                        existing.request = replace(existing.request,
                                                   quality_group=existing.request.quality_group | variant.quality_group)
                    else:
                        item = Submission(metadata, targets, variant)
                        group.append(item)
                        if variant.quality_rank is None:
                            direct[source_id] = item
                except DownloadCancelled:
                    raise
                except Exception as error:
                    failed(variant, error)

        with auth.worker_session(snapshot, settings) as session:
            try:
                expand(session, request)
            except DownloadCancelled:
                raise
            except Exception as error:
                failed(request, error)
            # 只吸收当前来源/关联季的其他普通输入；独立投稿留给后续发现。
            changed = True
            while changed:
                changed = False
                members = set().union(*(item.request.quality_group for item in group))
                for index, candidate in enumerate(pending):
                    if candidate is None or candidate.quality_rank is not None:
                        continue
                    candidate_members = {api.BANGUMI_PATH.fullmatch(urlsplit(value).path)['id']
                                         for value in related.get(candidate.url, [])}
                    if not any(matches(candidate, item) for item in group) and not members.intersection(candidate_members):
                        continue
                    pending[index] = None
                    changed = True
                    try:
                        expand(session, candidate)
                    except DownloadCancelled:
                        raise
                    except Exception as error:
                        failed(candidate, error)
        if group:
            yield group
