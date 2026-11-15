"""隔离核对已安装客户端的播放签名 ABI；请求和签名仅通过父子进程内存管道，不写文件。"""

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.playback.native_sign import playback_extra
from research.settings import load_research


def generate(settings: dict, library_path: Path, request: dict) -> str:
    """用 settings 描述的固定 ABI 调用 library_path；request 含 vid/guid/timestamp，返回仅驻内存的签名。"""
    if hashlib.sha256(library_path.read_bytes()).hexdigest() != settings["sha256"]:
        raise RuntimeError("签名 DLL 摘要与已核对 ABI 不符")
    with os.add_dll_directory(str(library_path.parent)):
        library = ctypes.WinDLL(str(library_path))
        function = getattr(library, settings["export"])
        function.argtypes = [ctypes.c_char_p] * 4 + [ctypes.c_int32, ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32), ctypes.c_int32, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int32)]
        function.restype = ctypes.c_void_p
        values = settings["extra_values"]
        array = (ctypes.c_int32 * len(values))(*values)
        extra = playback_extra(settings)
        length = ctypes.c_int32()
        pointer = function(request["vid"].encode(), settings["app_version"].encode(), request["guid"].encode(),
            settings["source"].encode(), settings["platform"], request["timestamp"], array, len(values),
            json.dumps(extra, separators=(",", ":")).encode(), b"", ctypes.byref(length))
        if not pointer or not 0 < length.value <= settings["max_output_bytes"]:
            raise RuntimeError("签名接口返回长度不符合已核对边界")
        # 已核对该导出用 IAT 中的 calloc 分配，必须由同一 CRT 的 free 释放。
        free_address = ctypes.c_void_p.from_address(library._handle + settings["free_import_rva"]).value
        free = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(free_address)
        try:
            return ctypes.string_at(pointer, length.value).decode("ascii")
        finally:
            free(pointer)


def main() -> None:
    """读取 --config 与 stdin JSON 的最小签名输入，向父进程输出内存结果；不访问播放网络、不写账号或签名文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    path = args.config.resolve()
    research, config = load_research(path)
    settings = research["native_observation"]["signing"]
    library = config["playback"]["native"]["dll_root"] / settings["client_version"] / settings["module"]
    print(json.dumps({"ckey": generate(settings, library, json.load(sys.stdin))}))


if __name__ == "__main__":
    main()
