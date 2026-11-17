"""通过隔离子进程读取客户端当前登录态；不采用历史 Cookie 或播放日志中的票据。"""

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import sys
from threading import Event, RLock
import time
from types import MappingProxyType
from typing import Callable, Mapping

from ..runtime import check_cancel
from .errors import AuthenticationError, AuthenticationRejected, AuthenticationStopped


def load_user(config: dict) -> dict:
    """按 config 在隔离进程读取原生用户对象；返回 tokens/app_version，异常不包含票据或 DLL 原始日志。"""
    command = [sys.executable, "-m", "local.playback.native_user", "--profile", str(config["playback_profile"])]
    try:
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[2],
                                capture_output=True, timeout=config["network"]["timeout_seconds"],
                                env={**os.environ, "PYTHONUTF8": "1"},
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise AuthenticationError("读取腾讯客户端登录态失败或超时") from None
    if result.returncode:
        raise AuthenticationError("客户端只读登录接口不可用；请检查登录 DLL 版本及专项配置")
    try:
        value = json.loads(result.stdout)
        if not value["tokens"]["vuserid"] or not value["tokens"]["vusession"]:
            raise ValueError("empty session")
        return value
    except (ValueError, KeyError, TypeError):
        raise AuthenticationError("客户端没有有效登录态，请先在腾讯客户端登录") from None


@dataclass(frozen=True)
class LoginSnapshot:
    """不可变登录快照；tokens 仅驻内存，app_version 为 DLL 版本，generation/loaded_at 标识代次和单调时刻。"""

    tokens: Mapping[str, str] = field(repr=False)
    app_version: str
    generation: int
    loaded_at: float


class LoginSession:
    """管理同一账号的登录快照；正常请求可并发，重读及拒绝后的重试在同一锁内完成。"""

    def __init__(self, config: dict, cancel: Event | None = None, *, user: dict | None = None,
                 loader: Callable | None = None, clock: Callable = time.monotonic):
        """按 config/cancel 初始化；user 为初始快照，loader(config)/clock() 可注入隔离替身，无返回值。"""
        self.config, self.cancel = config, cancel
        self.settings, self.clock = config["authentication"], clock
        self.loader = load_user if loader is None else loader
        self.lock, self.stopped = RLock(), Event()
        self.state = {"refreshes": 0, "recoveries": 0, "recovery_failures": 0}
        self.current = self._make_snapshot(self.loader(config) if user is None else user, 1)
        self.account = self._account(self.current)

    def _make_snapshot(self, user: dict, generation: int) -> LoginSnapshot:
        """校验 user 的登录结构并复制票据；generation 为正整数代次，返回只读内存快照。"""
        try:
            tokens = dict(user["tokens"])
            if (any(not isinstance(key, str) or not isinstance(value, str) for key, value in tokens.items())
                    or not tokens["vuserid"] or not tokens["vusession"]):
                raise ValueError("invalid tokens")
            version = str(user["app_version"])
        except (ValueError, KeyError, TypeError):
            raise AuthenticationError("客户端没有有效登录态，请先在腾讯客户端登录") from None
        return LoginSnapshot(MappingProxyType(tokens), version, generation, self.clock())

    @staticmethod
    def _account(snapshot: LoginSnapshot) -> tuple:
        """从 snapshot 取得仅供内存比较的账号/登录方式元组；返回值禁止写入报告。"""
        return snapshot.tokens["vuserid"], snapshot.tokens.get("main_login", "")

    @contextmanager
    def _locked(self):
        """以配置轮询间隔等待登录锁并响应取消；无参数，退出时释放锁。"""
        interval = self.config["runtime"]["cancel_poll_interval_seconds"]
        while not self.lock.acquire(timeout=interval):
            check_cancel(self.cancel)
        try:
            check_cancel(self.cancel)
            if self.stopped.is_set():
                raise AuthenticationStopped("认证恢复失败，当前任务停止请求并保留现场")
            yield
        finally:
            self.lock.release()

    def _read(self, reason: str) -> LoginSnapshot:
        """持锁重读客户端登录态；reason 是诊断原因，返回同账号新代次，切换账号时立即停止。"""
        snapshot = self._make_snapshot(self.loader(self.config), self.current.generation + 1)
        if self._account(snapshot) != self.account:
            self.state["last_refresh"] = {"reason": reason, "result": "account_changed"}
            self.stopped.set()
            raise AuthenticationError("客户端账号已切换，停止本批次；请确认账号后重新运行")
        changed = snapshot.tokens != self.current.tokens
        self.current = snapshot
        self.state["refreshes"] += 1
        self.state["last_refresh"] = {"reason": reason, "result": "read", "credentials_changed": changed}
        return snapshot

    def _fail(self) -> None:
        """将耗尽恢复机会的会话标记为终止并抛出安全异常；无参数或正常返回值。"""
        self.state["recovery_failures"] += 1
        self.stopped.set()
        raise AuthenticationError("客户端登录态重读及认证恢复失败；请在客户端重新登录后重新运行，断点已保留") from None

    def snapshot(self) -> LoginSnapshot:
        """返回当前不可变快照；到配置间隔时串行重读，读取失败有界重试后停止，不返回过期凭据。"""
        with self._locked():
            if self.clock() - self.current.loaded_at >= self.settings["refresh_interval_seconds"]:
                for _ in range(self.settings["recovery_attempts"]):
                    try:
                        return self._read("interval")
                    except AuthenticationError:
                        if self.stopped.is_set():
                            raise
                self.state["last_refresh"] = {"reason": "interval", "result": "read_failed"}
                self._fail()
            return self.current

    def execute(self, operation: Callable[[LoginSnapshot], dict]) -> dict:
        """使用单一快照调用 operation(snapshot)；仅明确认证拒绝触发恢复，返回认证后的原响应字典。"""
        snapshot = self.snapshot()
        try:
            return operation(snapshot)
        except AuthenticationRejected as error:
            return self._recover(operation, snapshot, error)

    def _recover(self, operation: Callable, rejected: LoginSnapshot, error: AuthenticationRejected) -> dict:
        """串行恢复 operation；rejected/error 标识被拒快照和安全诊断，返回成功结果或终止认证异常。"""
        with self._locked():
            self.state["last_rejection"] = {**error.details, "generation": rejected.generation,
                                            "snapshot_age_seconds": round(self.clock() - rejected.loaded_at, 3)}
            # 旧请求晚到的拒绝先使用已更新快照，不能覆盖其他线程的成功恢复或重复读取 DLL。
            if self.current.generation != rejected.generation:
                try:
                    return operation(self.current)
                except AuthenticationRejected as latest:
                    self.state["last_rejection"] = {**latest.details, "generation": self.current.generation}
            for _ in range(self.settings["recovery_attempts"]):
                try:
                    snapshot = self._read("rejected")
                    result = operation(snapshot)
                except AuthenticationStopped:
                    raise
                except AuthenticationRejected as latest:
                    self.state["last_rejection"] = {**latest.details, "generation": self.current.generation}
                except AuthenticationError:
                    if self.stopped.is_set():
                        raise
                    self.state["last_refresh"] = {"reason": "rejected", "result": "read_failed"}
                else:
                    self.state["recoveries"] += 1
                    print(f"[登录态] 已重读客户端登录态并恢复原请求（快照 {snapshot.generation}）", flush=True)
                    return result
            self._fail()

    def diagnostics(self) -> dict:
        """返回仅含代次、年龄、计数和白名单协议字段的副本；不读取或暴露票据及账号标识。"""
        with self.lock:
            return {**{key: dict(value) if isinstance(value, dict) else value for key, value in self.state.items()},
                    "generation": self.current.generation,
                    "snapshot_age_seconds": round(self.clock() - self.current.loaded_at, 3),
                    "stopped": self.stopped.is_set()}
