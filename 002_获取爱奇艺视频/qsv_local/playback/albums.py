"""按专辑获取整剧上映日期；日期在单个运行批次内固定，不使用单集或更新日期代替。"""

from datetime import datetime, timedelta, timezone
import json
from threading import Lock
from urllib.request import Request

from .network import open_network


class AlbumMetadataCache:
    """保存单个配置实例的专辑上映日期，串行首次请求以避免多集并发重复查询。"""

    def __init__(self):
        """创建空缓存及请求锁；无参数或返回值。"""
        self.rows = {}
        self.lock = Lock()

    def get(self, profile: dict, album_id: str) -> dict:
        """按 profile 查询或复用正整数编号 album_id；返回独立的上映日期及原始毫秒时间戳，字段无效时停止映射。"""
        if not isinstance(album_id, str) or not album_id.isascii() or not album_id.isdecimal() or int(album_id) <= 0:
            raise ValueError("专辑编号必须为正整数字符串")
        with self.lock:
            if album_id not in self.rows:
                network = profile["network"]
                url = network["album_info_url"].format(album_id=album_id)
                with open_network(profile, Request(url, headers={"User-Agent": network["user_agent"]})) as response:
                    raw = response.read(network["max_response_bytes"] + 1)
                if len(raw) > network["max_response_bytes"]:
                    raise ValueError("专辑资料超过配置的响应上限")
                payload = json.loads(raw.decode("utf-8-sig"))
                data = payload.get("data")
                if payload.get("code") != "A00000" or not isinstance(data, dict) or str(data.get("albumId")) != album_id:
                    raise ValueError("官方专辑资料与目标专辑编号不一致")
                value = data.get("publishTime")
                if type(value) is not int or value == 0:
                    raise ValueError("官方专辑缺少有效的 publishTime 上映日期，不使用单集或更新日期代替")
                try:
                    # 用 UTC 纪元加毫秒，兼容 Windows 上早于 1970 年的有效日期；展示偏移由配置提供。
                    instant = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=value)
                    zone = timezone(timedelta(minutes=network["album_date_utc_offset_minutes"]))
                    day = instant.astimezone(zone).date().isoformat()
                except (OverflowError, ValueError):
                    raise ValueError("官方专辑 publishTime 超出有效日期范围") from None
                self.rows[album_id] = {"release_date": day, "album_publish_time": value}
            return dict(self.rows[album_id])
