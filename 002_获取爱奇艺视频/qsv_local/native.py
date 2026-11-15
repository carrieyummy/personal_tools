"""通过本机已校验的 SDK 处理 v5 授权；票据和密钥不写入日志。"""

import ctypes
import hashlib
import json
import os
from pathlib import Path


class NativeV5:
    """持有设备和播放上下文；仅适配配置中 SHA256 对应的 Windows x64 DLL。"""

    def __init__(self, profile: dict, ticket: bytes):
        """加载 SDK；profile 提供 DLL、证书及函数 RVA，ticket 为 Base64 授权字节串。"""
        self.device = None
        self.session = None
        self.directory = None
        native = Path(profile["dll_path"])
        with native.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != profile["dll_sha256"]:
            raise ValueError("客户端 DLL 已变化，不能使用当前函数位置配置")
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
        """绑定函数；offset 为已校验 DLL 的 RVA，result/arguments 为 ctypes 类型；返回可调用函数。"""
        return ctypes.CFUNCTYPE(result, *arguments)(self.dll._handle + offset)

    @staticmethod
    def _check(status: int, operation: str) -> None:
        """检查 SDK 返回值；status 必须为零，operation 是可公开的操作名；失败时抛出异常。"""
        if status != 0:
            raise ValueError(f"SDK {operation} failed: {status}")

    def configure(self, descriptor: bytes) -> None:
        """配置 TS 分段；descriptor 为 SDT 中以竖线结尾的完整 ASCII 描述符，字段差异由已校验 SDK 解释，无返回值。"""
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
