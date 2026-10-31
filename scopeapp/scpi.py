"""SCPI 引擎：IEEE 488.2 二进制块解析、统一 ask/write、错误查询。"""
from __future__ import annotations

import re
import threading
from typing import Optional

from .model import ScopeIdentity
from .transport import Transport, TransportError

# #N<length><payload> 的 IEEE 488.2 块头，N 为长度数字的位数
_BLOCK_RE = re.compile(rb"^#([0-9])([0-9]+)")


class ScpiError(Exception):
    pass


def parse_ieee_block(data: bytes) -> tuple[bytes, int]:
    """解析 IEEE 488.2 二进制块头。

    返回 (payload, payload_offset)。data 以 b"#N<len>" 开头时按块解析，
    否则按纯数据返回（ASCII 波形等）。
    """
    if not data.startswith(b"#"):
        return data, 0
    if len(data) < 2 or not data[1:2].isdigit():
        raise ScpiError("无效的 IEEE 波形块头")
    ndigits = int(data[1:2])
    if ndigits == 0:
        raise ScpiError("不支持无长度的 IEEE 波形块")
    offset = 2 + ndigits  # '#' + 位数数字 + 长度数字
    size = data[2:offset]
    if len(size) != ndigits or not size.isdigit():
        raise ScpiError("IEEE 波形块长度字段不完整")
    length = int(size)
    if len(data) < offset + length:
        raise ScpiError(f"波形块截断：期望 {length} 字节，实际 {len(data) - offset} 字节")
    payload = data[offset : offset + length]
    return payload, offset


class ScpiEngine:
    """面向 SCPI 仪器的薄封装：超时保护、错误查询、*IDN? 解析。

    线程安全：所有设备通信经可重入锁串行化——采集 worker 与 UI 截图/
    控制并发访问同一台设备时不会互相干扰（一次请求-响应不可分割）。
    """

    def __init__(self, transport: Transport):
        self.transport = transport
        self._connected = False
        self.identity: Optional[ScopeIdentity] = None
        self._lock = threading.RLock()
        self.before_operation = None
        #: SCPI 通信日志（环境变量 SCOPEHOST_LOG 指向日志文件；用于排查
        #: "谁改写了设备状态"类问题，默认关闭）
        import os

        log_path = os.environ.get("SCOPEHOST_LOG", "")
        self._log = open(log_path, "a", encoding="utf-8") if log_path else None

    def _log_cmd(self, kind: str, cmd: str, resp: str = "") -> None:
        if self._log is None:
            return
        import time as _t

        ts = _t.strftime("%H:%M:%S")
        if kind == "W":
            self._log.write(f"[{ts}] W {cmd}\n")
        else:
            r = resp if len(resp) <= 80 else f"<{len(resp)}B> {resp[:80]}"
            self._log.write(f"[{ts}] A {cmd} -> {r}\n")
        self._log.flush()

    # ---------- 连接 ----------
    def connect(self) -> ScopeIdentity:
        self.transport.connect()
        self._connected = True
        try:
            idn = self.ask("*IDN?")
            self.identity = ScopeIdentity.parse_idn(idn)
            if not self.identity.manufacturer or not self.identity.model:
                raise ScpiError(f"设备身份响应不完整：{idn!r}")
        except Exception:
            self.disconnect()
            raise
        return self.identity

    def disconnect(self) -> None:
        if self._connected:
            with self._lock:
                self.transport.disconnect()
            self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ---------- 基础命令 ----------
    def write(self, cmd: str) -> None:
        with self._lock:
            if self.before_operation:
                self.before_operation()
            self._log_cmd("W", cmd)
            self.transport.write(cmd)

    def ask(self, cmd: str) -> str:
        with self._lock:
            if self.before_operation:
                self.before_operation()
            self._log_cmd("A", cmd)
            resp = self.transport.ask(cmd)
            self._log_cmd("R", cmd, resp)
            return resp

    def ask_binary(self, cmd: str) -> bytes:
        with self._lock:
            if self.before_operation:
                self.before_operation()
            self._log_cmd("A", cmd)
            resp = self.transport.ask_binary(cmd)
            self._log_cmd("R", cmd, f"<{len(resp)}B>")
            return resp

    def ask_binary_with_timeout(self, cmd: str, timeout_ms: int) -> bytes:
        with self._lock:
            if self.before_operation:
                self.before_operation()
            self._log_cmd("A", cmd)
            method = getattr(self.transport, "ask_binary_with_timeout", None)
            resp = method(cmd, timeout_ms) if method else self.transport.ask_binary(cmd)
            self._log_cmd("R", cmd, f"<{len(resp)}B>")
            return resp

    def ask_float(self, cmd: str, default: float = 0.0) -> float:
        try:
            return float(self.ask(cmd).strip())
        except (ValueError, TransportError):
            return default

    def ask_int(self, cmd: str, default: int = 0) -> int:
        try:
            return int(float(self.ask(cmd).strip()))
        except (ValueError, TransportError):
            return default

    def clear(self) -> None:
        try:
            self.write("*CLS")
        except TransportError:
            pass

    def check_error(self) -> str:
        """查询并清空错误队列，返回首个错误文本（无错误返回空串）。"""
        try:
            resp = self.ask("SYSTem:ERRor?").strip()
        except TransportError:
            return ""
        if resp.startswith("0"):
            return ""
        return resp
