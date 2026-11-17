"""只读连接当前优酷主进程；按生命周期回收本次 Inspector。"""
from __future__ import annotations
import ctypes
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
import websocket

def process_executable(pid: int) -> Path:
    """只读查询 Windows 进程路径；pid 为正进程号；返回完整路径，查询失败抛 OSError。"""
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise OSError("无法只读查询目标进程")
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise OSError("无法取得目标进程路径")
        return Path(buffer.value)
    finally:
        kernel.CloseHandle(handle)

class LiveInspector:
    """管理一次本机调试连接；只关闭本次激活的 Inspector，不关闭客户端或既有调试器。"""

    def __init__(self, config: dict[str, Any], pid: int) -> None:
        """初始化连接状态；config 含 live 参数，pid 为明确选择的优酷主进程号；不执行外部操作。"""
        self.config, self.pid = config, pid
        self.options = config["live"]
        self.ws = None
        self.sequence = 0
        self.activated = False
        self.closed_owned_inspector = False

    def discover(self) -> list[dict[str, Any]]:
        """查询配置指定的回环调试端点；无参数；返回目标列表，端点未就绪时返回空列表。"""
        try:
            with build_opener(ProxyHandler({})).open(self.options["inspector_url"],
                                                    timeout=self.options["timeout_seconds"]) as response:
                data = json.loads(response.read(self.options["max_result_bytes"]))
            return data if isinstance(data, list) else []
        except OSError:
            return []

    def __enter__(self) -> LiveInspector:
        """验证进程、按需临时开启调试并连接；无参数；返回自身，失败时清理本次连接。"""
        expected = self.config["client_dir"] / self.config["native"]["executable"]
        if process_executable(self.pid).resolve() != expected.resolve():
            raise ValueError("目标 PID 的程序路径与配置中的优酷客户端不一致")
        targets = self.discover()
        if not targets:
            env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
            run = subprocess.run([str(expected), "-e", "process._debugProcess(Number(process.argv[1]))", str(self.pid)],
                                 cwd=self.config["client_dir"], env=env, capture_output=True,
                                 timeout=self.options["timeout_seconds"], check=False)
            if run.returncode:
                raise ValueError("客户端未接受临时调试请求")
            self.activated = True
            for _ in range(self.options["connect_attempts"]):
                targets = self.discover()
                if targets:
                    break
                time.sleep(self.options["retry_interval_seconds"])
        try:
            if len(targets) != 1:
                raise ValueError("需要唯一的本机 Node Inspector 目标")
            target_url = targets[0]["webSocketDebuggerUrl"]
            endpoint, target = urlsplit(self.options["inspector_url"]), urlsplit(target_url)
            if target.scheme != "ws" or target.hostname != endpoint.hostname or target.port != endpoint.port:
                raise ValueError("调试 WebSocket 必须与配置的回环端点一致")
            self.ws = websocket.create_connection(target_url, timeout=self.options["timeout_seconds"],
                                                   suppress_origin=True, http_no_proxy=[target.hostname])
            identity = self.evaluate("({pid:process.pid,exe:process.execPath})")
            if identity["pid"] != self.pid or Path(identity["exe"]).resolve() != expected.resolve():
                # 身份不符时绝不向另一个进程发送关闭调试命令。
                self.activated = False
                raise ValueError("调试端点的进程身份不匹配")
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def evaluate(self, expression: str) -> Any:
        """执行研究内置表达式；expression 为内部 JS；返回内存结果，服务端异常只报告固定错误，不泄露内容。"""
        self.sequence += 1
        try:
            self.ws.send(json.dumps({"id": self.sequence, "method": "Runtime.evaluate",
                                     "params": {"expression": expression, "returnByValue": True, "awaitPromise": True}}))
        except websocket.WebSocketException as error:
            raise ValueError("实时调试发送失败：" + type(error).__name__) from None
        while True:
            try:
                raw = self.ws.recv()
            except websocket.WebSocketException as error:
                raise ValueError("实时调试接收失败：" + type(error).__name__) from None
            if len(raw) > self.options["max_result_bytes"]:
                raise ValueError("实时调试响应超过配置上限")
            message = json.loads(raw)
            if message.get("id") != self.sequence:
                continue
            result = message.get("result", {})
            if message.get("error") or result.get("exceptionDetails"):
                description = result.get("exceptionDetails", {}).get("exception", {}).get("description", "")
                reasons = ("SampleMismatch", "OriginMismatch", "RequestNotAvailable", "PlayerNotUnique")
                reason = next((name for name in reasons if name in description), "EvaluationFailed")
                raise ValueError("运行中客户端未能完成研究表达式：" + reason)
            return result.get("result", {}).get("value")

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """回收研究连接和本次开启的 Inspector；参数来自上下文管理协议；不抑制原始异常。"""
        close_requested = False
        try:
            if self.ws and self.activated:
                delay = self.options["close_delay_ms"]
                self.evaluate("setTimeout(()=>process.mainModule.require('inspector').close(),"
                              + str(delay) + "); true")
                close_requested = True
        finally:
            if self.ws:
                self.ws.close()
                self.ws = None
        if close_requested:
            for _ in range(self.options["connect_attempts"]):
                time.sleep(self.options["retry_interval_seconds"])
                if not self.discover():
                    self.closed_owned_inspector = True
                    break
            if not self.closed_owned_inspector:
                raise ValueError("未能确认本次开启的调试端口已关闭")

def evaluate_player(config: dict[str, Any], inspector: LiveInspector, expression: str) -> Any:
    """在唯一播放页执行内置表达式；config 为配置，inspector 为已校验连接，expression 为内部 JS；返回内存结果。"""
    script = "(()=>{const w=process.mainModule.require('electron').webContents.getAllWebContents().filter(w=>w.getURL().startsWith("
    script += json.dumps(config["live"]["player_url_prefix"]) + "));if(w.length!==1)throw Error('PlayerNotUnique');"
    script += "return w[0].executeJavaScript(" + json.dumps(expression) + ");})()"
    return inspector.evaluate(script)
