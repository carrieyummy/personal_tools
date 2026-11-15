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
