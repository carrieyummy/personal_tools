"""在显式指定的运行客户端中对照原生签名，复用正式流程的 ABI 校验、内存释放与自动分离。"""

from local.playback.native_sign import generate as native_key


def generate(config: dict, pid: int, request: dict) -> str:
    """用 config 的协议参数补足 request，并在已核对的 pid 上下文签名；返回仅驻内存的字符串。"""
    playback = config["playback"]["playback"]
    return native_key(config, {**request, "app_version": playback["app_version"],
        "platform": int(playback["query"]["platform"]), "source": playback["query"]["sdtfrom"]}, pid=pid)["ckey"]
