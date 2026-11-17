"""按已核验的专辑、集序及源名称生成安全的输出名称。"""

import re


def validate_component(value: str) -> str:
    """校验 value 为无空白的 Windows 单层名称；返回原值，非法字符或保留设备名时抛错。"""
    if (not isinstance(value, str) or not value or value in (".", "..") or value.endswith(".")
            or re.search(r'[\s<>:"/\\|?*\x00-\x1f]', value)
            or value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}):
        raise ValueError(f"输出名称无效：{value!r}")
    return value


def safe_component(value: str) -> str:
    """将非空源站名称 value 中的空白及非法字符替换为下划线；返回安全名称，空名称报错。"""
    if not isinstance(value, str):
        raise ValueError("客户端下载名称缺失")
    return validate_component(re.sub(r'[\s<>:"/\\|?*\x00-\x1f]+', "_", value).strip("._"))


def output_name(naming: dict, metadata: tuple, source_stem: str) -> tuple[str, str]:
    """根据 naming 模板、(专辑 ID, 剧名, 集序) metadata 和源文件/节目名称 source_stem 返回安全目录名及文件名。"""
    album_id, title, order = metadata
    if not isinstance(album_id, str) or re.fullmatch(r"[0-9]+", album_id) is None or int(album_id) <= 0:
        raise ValueError("客户端下载记录缺少有效的 AlbumID")
    if not isinstance(order, str) or re.fullmatch(r"[0-9]+", order) is None or int(order) <= 0:
        raise ValueError("客户端下载记录缺少有效的 vedioorder 集序")
    folder = naming["series_template"].format(title=safe_component(title), series_id=album_id)
    episode = naming["episode_template"].format(index=int(order), title=safe_component(source_stem))
    return validate_component(folder), validate_component(episode)
