"""在独立 64 位 Python 进程复用已核验的本机原生函数；标准输入/输出仅传递内存材料，不写文件。"""

import base64
import ctypes
import hashlib
import json
import os
import struct
import sys
from pathlib import Path


def derive_key(request: dict) -> bytes:
    """校验库版本后调用其纯计算函数；request 含库路径、摘要、RVA、输出上限和当前三元组；返回 16 字节内存结果。"""
    library = Path(request["library"])
    raw = library.read_bytes()
    if hashlib.sha256(raw).hexdigest() != request["sha256"]:
        raise ValueError("NativeLibraryVersionMismatch")
    pe_offset = struct.unpack_from("<I", raw, 0x3C)[0]
    if raw[pe_offset:pe_offset + 4] != b"PE\0\0" or struct.unpack_from("<H", raw, pe_offset + 4)[0] != 0x8664 or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError("NativeLibraryArchitectureMismatch")
    section_count = struct.unpack_from("<H", raw, pe_offset + 6)[0]
    optional_size = struct.unpack_from("<H", raw, pe_offset + 20)[0]
    table = pe_offset + 24 + optional_size
    executable = False
    for index in range(section_count):
        entry = table + index * 40
        size, address = struct.unpack_from("<II", raw, entry + 8)
        flags = struct.unpack_from("<I", raw, entry + 36)[0]
        executable |= bool(flags & 0x20000000 and address <= request["function_rva"] < address + size)
    if not executable or not 25 <= request["output_bytes"] <= 4096:
        raise ValueError("NativeFunctionBoundaryInvalid")
    triple = request["material"]
    if len(triple) != 3 or any(not isinstance(value, str) or not value or "," in value or "\0" in value for value in triple):
        raise ValueError("NativeMaterialInvalid")
    with os.add_dll_directory(str(library.parent)):
        dll = ctypes.WinDLL(str(library))
        function = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int)(
            dll._handle + request["function_rva"])
        output = ctypes.create_string_buffer(request["output_bytes"])
        returned = function(",".join(triple).encode("utf-8"), output, len(output))
        if not returned or len(output.value) != 24:
            raise ValueError("NativeResultInvalid")
        key = base64.b64decode(output.value, validate=True)
        if len(key) != 16:
            raise ValueError("NativeResultInvalid")
        return key


def main() -> int:
    """读取父进程内存协议并返回固定前缀结果；无参数，成功返回 0；只报告异常类型，不输出授权值或原生日志。"""
    try:
        request = json.load(sys.stdin)
        key = derive_key(request)
        result = {"key": base64.b64encode(key).decode("ascii")}
        code = 0
    except Exception as error:
        result, code = {"error": type(error).__name__}, 1
    print("YOUKU_KEY_RESULT=" + json.dumps(result), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
