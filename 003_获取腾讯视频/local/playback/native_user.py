"""隔离读取客户端 UserData 的只读接口；票据仅通过父子进程内存管道传递。"""

import argparse
import ctypes
import json
import os
from pathlib import Path
import uuid

import yaml

from ..configuration import resolve_path
from ..native import latest_dll, dll_digest


def guid(value: str):
    """将带连字符 UUID 字符串 value 转成 Windows GUID 字节数组并返回。"""
    return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)


def method(pointer, index: int, result, *args):
    """为 COM pointer 的 index 槽构造 result/args 描述的函数；返回绑定调用器。"""
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *args)(table[index])


def read_property(dll_path: Path, class_id: str, name: str, interface_id: str, slot: int) -> dict:
    """读取 dll_path/class_id 的 name 属性；interface_id/slot 来自类型库，返回内存 JSON 对象。"""
    ole = ctypes.OleDLL("ole32")
    ole.CoInitializeEx(None, 2)
    factory, dispatch = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        with os.add_dll_directory(str(dll_path.parent)):
            library = ctypes.WinDLL(str(dll_path))
            create = library.DllGetClassObject
            create.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            create.restype = ctypes.c_long
            status = create(guid(class_id), guid("00000001-0000-0000-c000-000000000046"), ctypes.byref(factory))
            if status < 0:
                raise RuntimeError(f"取得客户端类工厂失败：{status}")
            status = method(factory, 3, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_void_p))(factory, None,
                            guid(interface_id), ctypes.byref(dispatch))
            if status < 0:
                raise RuntimeError(f"取得客户端只读接口失败：{status}")
            result = ctypes.c_void_p()
            status = method(dispatch, slot, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                dispatch, ctypes.byref(result))
            if status < 0:
                raise RuntimeError(f"读取客户端属性失败：{status}")
            try:
                if not result:
                    raise RuntimeError("客户端属性未返回 BSTR")
                return json.loads(ctypes.wstring_at(result.value))
            finally:
                free = ctypes.WinDLL("oleaut32").SysFreeString
                free.argtypes, free.restype = [ctypes.c_void_p], None
                free(result)
    finally:
        for pointer in (dispatch, factory):
            if pointer:
                method(pointer, 2, ctypes.c_ulong)(pointer)
        ole.CoUninitialize()


def main() -> None:
    """从 --profile 读取并核验客户端适配；向父进程管道发送最小登录 JSON，不写凭据文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    path = args.profile.resolve()
    native = yaml.safe_load(path.read_text(encoding="utf-8-sig"))["native"]
    client_path = resolve_path(native["client_profile"], path.parent)
    client = yaml.safe_load(client_path.read_text(encoding="utf-8-sig"))
    dll = latest_dll(resolve_path(client["dll_root"], client_path.parent), native["user_library"])
    if dll_digest(dll) != native["user_library_sha256"]:
        raise RuntimeError("登录 DLL 版本未验证，请先更新只读接口适配")
    value = read_property(dll, native["user_class_id"], "UserDataJSON",
                          native["user_interface_id"], native["user_json_slot"])
    print(json.dumps({"tokens": {key: value.get(key, "") for key in native["token_fields"]},
                      "app_version": dll.parent.name}, ensure_ascii=True))


if __name__ == "__main__":
    main()
