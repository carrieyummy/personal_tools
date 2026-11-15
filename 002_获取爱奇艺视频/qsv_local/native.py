"""通过最新客户端 SDK 处理 v5 授权；票据和密钥不写入日志。"""

import ctypes
import json
import os
from pathlib import Path
import re


def latest_dll(root: Path, name: str) -> Path:
    """从 root 的数字版本子目录中选最新版的 name 文件；无版本目录或文件时抛错，返回 DLL 路径。

    root 为已解析的客户端目录，name 为 DLL 文件名；版本号按各数字段比较，不回退到旧版本。
    """
    versions = [path for path in root.iterdir() if path.is_dir() and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", path.name)]
    if not versions:
        raise FileNotFoundError(f"客户端目录中没有数字版本目录：{root}")
    folder = max(versions, key=lambda path: (tuple(map(int, path.name.split("."))), path.name))
    path = folder / name
    if not path.is_file():
        raise FileNotFoundError(f"最新客户端版本目录中不存在 DLL：{path}")
    return path


class NativeV5:
    """持有设备和播放上下文；使用最新客户端 DLL 和配置中的函数位置。"""

    def __init__(self, profile: dict, ticket: bytes):
        """加载 SDK；profile 提供客户端目录、DLL 名称、证书及函数 RVA，ticket 为 Base64 授权字节串。"""
        self.device = None
        self.session = None
        self.directory = None
        native = latest_dll(profile["dll_root"], profile["dll_name"])
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise ValueError("需要 64 位 Python")
        self.directory = os.add_dll_directory(str(native.parent))
        try:
            self.dll = ctypes.CDLL(str(native))
            offsets = profile["rva"]
            pointer, integer = ctypes.c_void_p, ctypes.c_int
            self.allocate = self._bind(offsets["allocate"], pointer)
            self.release = self._bind(offsets["release"], None, pointer)
            self.set_certificate = self._bind(offsets["set_certificate"], integer,
                                              pointer, ctypes.c_char_p, integer, ctypes.c_char_p, integer)
            self.set_license = self._bind(offsets["set_license"], integer,
                                         pointer, pointer, ctypes.c_char_p, integer)
            self.set_mode = self._bind(offsets["set_mode"], integer, pointer, integer, ctypes.c_char_p)
            self.decode = self._bind(offsets["decrypt"], integer, pointer, pointer,
                                    ctypes.POINTER(integer), pointer, integer)
            self.device, self.session = self.allocate(), self.allocate()
            if not self.device or not self.session:
                raise MemoryError("SDK 上下文分配失败")
            cert = json.loads(Path(profile["cert_path"]).read_text(encoding="utf-8"))["cert"].encode("ascii")
            self._check(self.set_certificate(self.device, cert, len(cert), b"", 0), "certificate")
            self._check(self.set_license(self.session, self.device, ticket, len(ticket)), "license")
        except BaseException:
            self.close()
            raise

    def _bind(self, offset: int, result, *arguments):
        """绑定函数；offset 为 DLL 的 RVA，result/arguments 为 ctypes 类型；返回可调用函数。"""
        return ctypes.CFUNCTYPE(result, *arguments)(self.dll._handle + offset)

    @staticmethod
    def _check(status: int, operation: str) -> None:
        """检查 SDK 返回值；status 必须为零，operation 是可公开的操作名；失败时抛出异常。"""
        if status != 0:
            raise ValueError(f"SDK {operation} failed: {status}")

    def configure(self, descriptor: bytes) -> None:
        """配置 TS 分段；descriptor 为 SDT 中以竖线结尾的完整 ASCII 描述符，字段差异由 SDK 解释，无返回值。"""
        if not descriptor.endswith(b"|") or b"\0" in descriptor:
            raise ValueError("无效的分段加密描述符")
        self._check(self.set_mode(self.session, 1, descriptor), "set_mode")

    def decrypt(self, data: bytes) -> bytes:
        """解密已移除防竞争字节的 NAL 负载；data 不含 NAL 头；返回通过 SDK 校验的明文。"""
        output = ctypes.create_string_buffer(len(data))
        size = ctypes.c_int(len(data))
        self._check(self.decode(self.session, output, ctypes.byref(size), data, len(data)), "decrypt")
        if not 0 <= size.value <= len(data):
            raise ValueError("SDK 返回非法输出长度")
        return output.raw[:size.value]

    def close(self) -> None:
        """释放本对象的 SDK 上下文和 DLL 搜索目录句柄；可重复调用，无返回值。"""
        for name in ("session", "device"):
            value = getattr(self, name, None)
            if value:
                self.release(value)
                setattr(self, name, None)
        if self.directory:
            self.directory.close()
            self.directory = None

    def __enter__(self):
        """进入资源作用域；返回当前 SDK 对象。"""
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """退出作用域并释放上下文；三个参数为 Python 异常信息；不抑制异常。"""
        self.close()
