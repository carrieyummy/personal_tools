"""调用客户端最新版本的 DLL 还原 HVN 缓存层；不提取密钥，不修改客户端或源文件。"""

import ctypes as C
import hashlib
import os
from pathlib import Path
import re
import struct


HEADER_SIZE = 18


class NativeFingerprintMismatch(RuntimeError):
    """表示最新客户端 DLL 的内容与已验证指纹不一致。"""


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


def dll_digest(path: Path) -> str:
    """计算 DLL 文件 path 的 SHA-256，返回小写十六进制摘要。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified_dll(profile: dict) -> Path:
    """选取 profile 指定的最新 DLL 并核对配置中的 SHA-256；匹配时返回路径，否则拒绝调用。"""
    path = latest_dll(profile["dll_root"], profile["dll_name"])
    if dll_digest(path) != profile["dll_sha256"]:
        raise NativeFingerprintMismatch(f"最新客户端 DLL 指纹与接口配置不匹配，需重新分析并验收：{path}")
    return path


class CacheLibrary:
    """持有最新客户端 DLL；函数位置和对象布局由专项配置提供。"""

    def __init__(self, profile: dict):
        """根据 profile 加载最新客户端目录中的 x64 DLL；平台或文件不可用时抛错。"""
        if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
            raise RuntimeError("本机缓存适配器需要 Windows x64 Python")
        self.profile = profile
        path = verified_dll(profile)
        self.directory = os.add_dll_directory(str(path.parent))
        self.dll = C.CDLL(str(path))
        rva = profile["rva"]
        self.construct = C.CFUNCTYPE(C.c_void_p, C.c_void_p)(self.dll._handle + rva["construct"])
        self.initialize = C.CFUNCTYPE(C.c_bool, C.c_void_p, C.c_char_p, C.c_bool)(self.dll._handle + rva["initialize"])
        self.checksum = C.CFUNCTYPE(C.c_uint32, C.c_void_p)(self.dll._handle + rva["checksum"])
        self.destroy = C.CFUNCTYPE(None, C.c_void_p)(self.dll._handle + rva["destroy"])


class CacheReader:
    """一个 HVN 文件的解密上下文；仅支持经配置和文件头校验的缓存版本。"""

    def __init__(self, library: CacheLibrary, header: bytes, resource_dir: Path):
        """以 library、完整 18 字节 header 和真实资源目录 resource_dir 初始化只读上下文。"""
        self.library = library
        self.context = None
        if len(header) != HEADER_SIZE or header[:4] != b"HVN\0":
            raise ValueError("分片不是完整的 HVN 缓存文件")
        if struct.unpack_from("<H", header, 4)[0] != library.profile["cache_version"]:
            raise ValueError("不支持此 HVN 缓存版本")
        self.context = C.create_string_buffer(library.profile["context_size"])
        library.construct(self.context)
        C.memmove(self.context, header, HEADER_SIZE)
        try:
            if not library.initialize(self.context, (resource_dir.as_posix() + "/").encode("utf-8"), False):
                raise ValueError("缓存读取上下文初始化失败")
            if library.checksum(self.context) != struct.unpack_from("<I", header, 14)[0]:
                raise ValueError("缓存文件头校验失败")
            self.crypto = C.c_void_p.from_buffer(self.context, library.profile["crypto_pointer_offset"]).value
            if not self.crypto:
                raise ValueError("客户端返回空缓存上下文")
            vtable = C.c_void_p.from_address(self.crypto).value
            address = C.c_void_p.from_address(vtable + library.profile["decrypt_vtable_offset"]).value
            self.decode = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p, C.c_int, C.c_int64,
                                     C.c_void_p, C.POINTER(C.c_int))(address)
        except BaseException:
            self.close()
            raise

    def decrypt(self, data: bytes, offset: int) -> bytes:
        """还原 data；offset 为不含 HVN 头的分片内非负字节偏移；返回等长媒体字节。"""
        if self.context is None or offset < 0 or not data or len(data) >= 2**31:
            raise ValueError("无效的缓存读取状态、长度或偏移")
        buffer = C.create_string_buffer(data)
        size = C.c_int(len(data))
        result = self.decode(self.crypto, buffer, len(data), offset, buffer, C.byref(size))
        if result != 0 or size.value != len(data):
            raise ValueError(f"缓存还原失败，返回码 {result}，输出长度 {size.value}")
        return buffer.raw[:size.value]

    def close(self) -> None:
        """释放当前分片上下文；允许重复调用，无返回值。"""
        if self.context is not None:
            self.library.destroy(self.context)
            self.context = None

    def __enter__(self):
        """进入分片读取作用域，返回当前对象。"""
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """根据 Python 上下文协议释放资源；异常参数为类型、对象和堆栈，不抑制异常。"""
        self.close()
