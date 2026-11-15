"""聚合可下载画质，确认关联季逐集档位排名或单任务上限，并限定各分 P 的实际下载流。"""

from copy import deepcopy
from itertools import product
from typing import Any

from .errors import ToolError
from .metadata import VideoInfo
from .models import Quality, entry_quality
from . import variants
from . import media


class QualityUnavailable(ToolError):
    """表示指定实际档位没有可下载流，供上层排除该档位或报告失败，不重新询问画质上限。"""


def quality_formats(info: dict[str, Any]) -> dict[int, tuple[Quality, list[dict[str, Any]]]]:
    """从 info 实际流按正整数平台编号聚合画质；排除音频、DRM、无地址项，返回编号到档位及格式的映射。"""
    result: dict[int, tuple[Quality, list[dict[str, Any]]]] = {}
    for fmt in info.get("formats", []):
        code = fmt.get("quality")
        if (type(code) is not int or code < 1 or fmt.get("vcodec") == "none"
                or fmt.get("has_drm") or not (fmt.get("url") or fmt.get("fragments"))):
            continue
        name = fmt.get("format_note") or fmt.get("format") or f"画质{code}"
        if code not in result:
            result[code] = (Quality(code, name), [])
        result[code][1].append(fmt)
    return result


def select_quality(downloader: Any, info: dict[str, Any], quality: Quality,
                   signature: tuple[tuple[Any, ...], ...] | None = None) -> dict[str, Any]:
    """按 downloader 配置选择 info 的 quality；signature 可锁定有序编码尺寸，返回已选流，缺失时报错。"""
    available = quality_formats(info)
    if quality.code not in available:
        raise QualityUnavailable(f"{info.get('id')} 不再支持 {quality.name}")
    restricted = deepcopy(info)
    restricted["formats"] = [fmt for fmt in restricted["formats"]
                             if not fmt.get("has_drm") and (fmt.get("url") or fmt.get("fragments"))
                             and (fmt.get("vcodec") == "none" or fmt.get("quality") == quality.code)]
    if signature is not None:
        restricted['formats'] = [fmt for fmt in restricted['formats'] if variants.compatibility([fmt])[0] in signature]
    # 重新选流前删除前次自动选流结果，避免 requested_formats 或顶层地址绕过所选档位。
    for key in ("requested_formats", "requested_downloads", "url", "format_id", "format", "ext",
                "vcodec", "acodec", "width", "height", "fps", "quality", "protocol"):
        restricted.pop(key, None)
    try:
        selected = downloader.process_ie_result(restricted, download=False)
    except Exception as exc:
        raise QualityUnavailable(f"{quality.name} 无法按当前 format 配置选流：{exc}") from exc
    if not selected or selected.get("_type", "video") != "video":
        raise ToolError("画质选择结果不是单个视频")
    formats = selected.get("requested_formats") or [selected]
    videos = [fmt for fmt in formats if fmt.get("vcodec") != "none"]
    if not videos or any(fmt.get("quality") != quality.code for fmt in videos):
        raise ToolError("实际选流与确认的清晰度不一致，已拒绝下载")
    selected["selected_quality"] = {"code": quality.code, "name": quality.name}
    if signature is not None and variants.compatibility(formats) != signature:
        raise QualityUnavailable(f'{quality.name} 的指定编码或分辨率已不可用')
    return selected


def available_variants(downloader: Any, info: dict[str, Any], options: dict[int, Quality]) -> dict[int, dict[tuple, variants.StreamChoice]]:
    """枚举 info 中 options 档位的兼容选流；遵守 downloader 的格式配置，返回按兼容签名索引的最佳候选。"""
    result = {}
    for code, quality in options.items():
        preferred = select_quality(downloader, info, quality)
        ordered = preferred.get('formats', info['formats'])
        ranks = {fmt.get('format_id'): index for index, fmt in enumerate(ordered)}
        video = list(dict.fromkeys(variants.compatibility([fmt])[0] for fmt in reversed(ordered)
                                  if fmt.get('quality') == code and fmt.get('vcodec') != 'none'
                                  and not fmt.get('has_drm') and (fmt.get('url') or fmt.get('fragments'))))
        audio = list(dict.fromkeys(variants.compatibility([fmt])[0] for fmt in reversed(ordered)
                                  if fmt.get('vcodec') == 'none' and not fmt.get('has_drm')
                                  and (fmt.get('url') or fmt.get('fragments'))))
        preferred_formats = preferred.get('requested_formats') or [preferred]
        layout = variants.compatibility(preferred_formats)
        signatures = list(dict.fromkeys([layout, *product(*(audio if signature[0] == 'none' else video for signature in layout)),
                                         *((signature,) for signature in video), *((v, a) for v in video for a in audio)]))
        candidates = {}
        for signature in signatures:
            try:
                selected = select_quality(downloader, info, quality, signature)
            except QualityUnavailable:
                continue
            formats = selected.get('requested_formats') or [selected]
            stable = tuple({key: fmt.get(key) for key in ('format_id', 'vcodec', 'acodec', 'width', 'height', 'fps')}
                           for fmt in formats)
            candidates[signature] = variants.StreamChoice(quality, stable, tuple(ranks.get(fmt.get('format_id'), -1) for fmt in formats),
                                                          **media.expected_size(formats))
        if candidates:
            result[code] = candidates
    return result


def common_choice(catalogs: dict[int, dict[int, dict[tuple, variants.StreamChoice]]], parts: list[int],
                  ceiling: Quality) -> dict[int, variants.StreamChoice]:
    """为 parts 在 catalogs 中寻找不超过 ceiling 的最高兼容方案；返回逐P候选，缺失时报错。"""
    unique = list(dict.fromkeys(parts))
    if any(part not in catalogs for part in unique):
        raise ToolError('拼接组包含未选择或清晰度查询失败的分 P')
    codes = set.intersection(*(set(catalogs[part]) for part in unique))
    for code in sorted((code for code in codes if code <= ceiling.code), reverse=True):
        signatures = set.intersection(*(set(catalogs[part][code]) for part in unique))
        if signatures:
            signature = max(signatures, key=lambda value: (tuple(catalogs[p][code][value].rank for p in unique), repr(value)))
            return {part: catalogs[part][code][signature] for part in unique}
    raise ToolError(f'画质上限 {ceiling.name} 内没有同档位且编码和分辨率兼容的方案；仍按清晰度优先下载')


def available_qualities(downloader: Any, info: dict[str, Any], *, size_info: dict[int, dict[str, Any]] | None = None) -> dict[int, Quality]:
    """检查 downloader 可选的 info 档位；可选 size_info 接收各档已选版本及大小，返回档位映射，不额外联网。"""
    result = {}
    for code, (quality, _) in quality_formats(info).items():
        try:
            selected = select_quality(downloader, info, quality)
        except QualityUnavailable:
            continue
        result[code] = quality
        if size_info is not None:
            formats = media.chosen_formats(selected)
            size_info[code] = {**media.expected_size(formats), 'format_variant': variants.variant_id(formats),
                               'formats': [{key: fmt.get(key) for key in ('format_id', 'vcodec', 'acodec', 'width', 'height', 'fps')}
                                           for fmt in formats]}
    return result


def read_choice(count: int, prompt: str, *, allow_skip: bool = False) -> int | None:
    """读取 1..count 的菜单序号；prompt 为提示，allow_skip 允许 s 跳过，q/EOF 取消，返回零基序号或 None。"""
    while True:
        try:
            value = input(prompt).strip().lower()
        except EOFError as exc:
            raise KeyboardInterrupt("没有收到清晰度确认") from exc
        if value == 'q':
            raise KeyboardInterrupt("用户取消清晰度确认")
        if allow_skip and value == 's':
            return None
        if value.isdecimal() and 1 <= int(value) <= count:
            return int(value) - 1
        print("请输入有效序号" + ("，s 跳过剩余分 P" if allow_skip else "") + "，或 q 取消；不会默认选择画质。")


def below_ceiling(options: dict[int, Quality], ceiling: Quality) -> Quality:
    """从实际可用档位 options 选择不超过 ceiling 平台编号的最高档位，返回 Quality；没有可用档位时报错。"""
    allowed = [choice for code, choice in options.items() if code <= ceiling.code]
    if not allowed:
        raise ToolError(f'画质上限 {ceiling.name} (qn={ceiling.code}) 内没有可下载档位')
    return max(allowed, key=lambda choice: choice.code)


def confirm_rank(catalogs: dict[tuple[str, int], dict[int, Quality]]) -> int | None:
    """为 catalogs 关联季确认一次正整数档位排名，键为(ss号,P号)，各集按平台编号降序独立排名。

    返回从1开始的排名，跳过整组或无档位时返回 None；q/EOF 取消，缺少所选排名的集由调用方报告失败。
    """
    ordered = {part: sorted(options.values(), key=lambda choice: choice.code, reverse=True)
               for part, options in catalogs.items()}
    count = max((len(options) for options in ordered.values()), default=0)
    if not count:
        print('没有可下载清晰度')
        return None
    parts = '、'.join(f'{season} P{part}' for season, part in sorted(catalogs))
    print(f'\n待确认剧集：{parts}；选择一次档位排名，各集分别按可下载画质降序取档：')
    for rank in range(1, count + 1):
        choices = [options[rank - 1] for options in ordered.values() if len(options) >= rank]
        names = '、'.join(dict.fromkeys(f'{choice.name} (qn={choice.code})' for choice in choices))
        print(f'  {rank}. 第 {rank} 高档位 | {names} | 本档支持 {len(choices)}/{len(catalogs)} 集')
    index = read_choice(count, '输入画质档位（1=各集最高），s 跳过本番剧全部关联季，q 取消：', allow_skip=True)
    return index + 1 if index is not None else None


def confirm_ceiling(catalogs: dict[int, dict[int, Quality]], *, per_group: bool = False) -> Quality | None:
    """对 catalogs 并集只询问一次上限；per_group 标明组内兼容选流，返回Quality或跳过时None，q/EOF取消。"""
    union = {code: choice for options in catalogs.values() for code, choice in options.items()}
    choices = sorted(union.values(), key=lambda choice: choice.code, reverse=True)
    if not choices:
        print('没有可下载清晰度')
        return None
    behavior = '各拼接组独立选择上限内的最高兼容方案' if per_group else '其他分 P 自动使用上限内的最高档'
    print(f'\n待确认分 P：{sorted(catalogs)}；选择一次画质上限，{behavior}：')
    for index, choice in enumerate(choices, 1):
        exact = [part for part, options in catalogs.items() if choice.code in options]
        print(f'  {index}. {choice.name} (qn={choice.code}) | 本档支持 {len(exact)}/{len(catalogs)} P')
    index = read_choice(len(choices), '输入画质上限序号，s 跳过本任务，q 取消：', allow_skip=True)
    return choices[index] if index is not None else None


def confirm_qualities(catalogs: dict[int, dict[int, Quality]]) -> tuple[dict[int, Quality], list[int]]:
    """为 catalogs 选择一次统一上限后自动分配各 P；返回实际档位映射和无可用档位或主动跳过的 P 列表。"""
    ceiling = confirm_ceiling(catalogs)
    assignments, skipped = {}, []
    for part, options in catalogs.items():
        try:
            if ceiling is None:
                raise ToolError('用户跳过或没有可下载档位')
            assignments[part] = below_ceiling(options, ceiling)
        except ToolError:
            skipped.append(part)
    print('\n下载清单：' + '；'.join(f'P{part} → {choice.name}' for part, choice in sorted(assignments.items())))
    return assignments, sorted(skipped)


def choose_merge_quality(record: VideoInfo) -> Quality:
    """列出 record 中已完成的本地分 P 档位，单档自动使用、多档交互选择，返回拼接使用的档位。"""
    choices = {entry_quality(item).code: entry_quality(item) for item in record.data["files"] if item.get("kind") == "part" and item.get("status") == "completed"}
    if not choices:
        raise ToolError("没有已完成的本地分 P 可供拼接")
    ordered = sorted(choices.values(), key=lambda quality: quality.code or -1, reverse=True)
    if len(ordered) == 1:
        return ordered[0]
    print("\n请选择本次拼接使用的清晰度（不会跨目录补齐）：")
    for index, quality in enumerate(ordered, 1):
        parts = sorted(item["part"] for item in record.data["files"] if item.get("kind") == "part" and item.get("status") == "completed" and entry_quality(item).code == quality.code)
        print(f"  {index}. {quality.name} | 已完成分 P：{parts}")
    return ordered[read_choice(len(ordered), "输入清晰度序号，q 取消：")]


def choose_local_sources(record: VideoInfo, quality: Quality, parts: list[int] | None) -> dict[int, str] | None:
    """为 record 的 quality/parts 选择本地兼容版本；多方案交互，返回P到标识，单版本旧记录返回 None。"""
    entries = [item for item in record.data['files'] if item.get('kind') == 'part'
               and entry_quality(item).code == quality.code and (parts is None or item.get('part') in parts)]
    numbers = list(dict.fromkeys(parts)) if parts is not None else sorted({item['part'] for item in entries})
    if not any(sum(item['part'] == part for item in entries) > 1 for part in numbers):
        return None
    families = {}
    for item in entries:
        if item.get('status') == 'completed' and item.get('formats'):
            families.setdefault(variants.compatibility(item['formats']), {}).setdefault(item['part'], []).append(item)
    choices = sorted((key for key, mapping in families.items() if all(part in mapping for part in numbers)), key=repr)
    if not choices:
        raise ToolError('所选分 P 没有共有的本地兼容编码版本')
    index = 0
    if len(choices) > 1:
        print('\n请选择本组的兼容编码：')
        for index, signature in enumerate(choices, 1):
            print(f'  {index}. {signature}')
        index = read_choice(len(choices), '输入编码序号，q 取消：')
    mapping, result = families[choices[index]], {}
    for part in numbers:
        options = sorted(mapping[part], key=lambda item: item['id'])
        selected = 0
        if len(options) > 1:
            print(f'\nP{part} 的可兼容版本：')
            for index, item in enumerate(options, 1):
                print(f'  {index}. {item.get("file_name")}')
            selected = read_choice(len(options), '输入版本序号，q 取消：')
        result[part] = options[selected]['id']
    return result
