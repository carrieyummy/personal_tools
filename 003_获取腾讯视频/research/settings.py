"""为原生协议研究复用正式配置的 DLL 适配与能力声明，避免两处维护签名常量。"""

from copy import deepcopy
from pathlib import Path

import yaml

from local.configuration import load_config, resolve_path


def load_research(path: Path) -> tuple[dict, dict]:
    """读取研究 YAML path 及其主配置，合并共享签名适配；返回研究配置与已解析主配置两个独立字典。"""
    research = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    config = load_config(resolve_path(research["config"], path.parent), source="url")
    playback = config["playback"]["playback"]
    research["native_observation"]["signing"] = {**deepcopy(playback["ckey"]), **research["native_observation"]["signing"],
        "app_version": playback["app_version"], "platform": int(playback["query"]["platform"]), "source": playback["query"]["sdtfrom"]}
    return research, config


def sample_requests(config: dict) -> list:
    """从 config 清单保序选取每个链接的单集样本；无 vid 的整剧链接取目录首集，返回请求列表，不按备注筛选。"""
    from dataclasses import replace
    from types import SimpleNamespace
    from local.playback.client import public_catalog_page
    from local.playback.tasks import catalog, requests
    from local.playback.transport import Transport

    transport = Transport(config)
    def page(cid, vid, context=None, page_id=None):
        """通过公开接口查询 cid/vid 及可选分页 context/page_id；返回目录，不读取登录态。"""
        return public_catalog_page(config["playback"], transport, cid, vid, context, page_id)
    client = SimpleNamespace(profile=config["playback"], catalog_page=page)
    result = []
    for item in requests(config, require_single_video=False):
        if not item.video_id:
            task = catalog(client, replace(item, all_parts=True))[0]
            item = replace(item, series_id=task["series_id"], video_id=task["video_id"])
        result.append(replace(item, all_parts=False))
    return result
