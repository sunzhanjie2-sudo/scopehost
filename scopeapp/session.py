"""会话层：统一管理 传输层 + SCPI 引擎 + 适配器，GUI 与 CLI 共用。"""
from __future__ import annotations

from typing import Optional
import threading

from .adapters import ScopeAdapter, create_adapter
from .scpi import ScpiEngine
from .transport import TcpTransport, Transport
from .visa_process import VisaProcessTransport


class ScopeSession:
    """一次示波器连接会话。"""

    def __init__(self) -> None:
        self.transport: Optional[Transport] = None
        self.engine: Optional[ScpiEngine] = None
        self.adapter: Optional[ScopeAdapter] = None
        self._pending_transport = None
        self._cancelled = threading.Event()

    def cancel_connect(self):
        self._cancelled.set()
        transport = self._pending_transport or self.transport
        if transport and hasattr(transport, "cancel"):
            transport.cancel()

    def _check_cancelled(self):
        if self._cancelled.is_set():
            raise RuntimeError("连接已取消")

    @property
    def connected(self) -> bool:
        return self.adapter is not None and self.engine is not None and self.engine.connected

    def connect_tcp(self, host: str, port: int = 5025, adapter_name: Optional[str] = None) -> ScopeAdapter:
        """连接 LAN/TCP 示波器，按 *IDN? 自动识别适配器。"""
        return self._connect(TcpTransport(host, port), adapter_name)

    def connect_visa(self, resource: str, adapter_name: Optional[str] = None, backend_spec: str = "") -> ScopeAdapter:
        """连接 VISA 资源（USB-TMC / 串口 / TCPIP）。"""
        return self._connect(VisaProcessTransport(resource, backend_spec=backend_spec), adapter_name)

    def connect_simulator(self, port: int = 5025, adapter_name: Optional[str] = None) -> ScopeAdapter:
        """连接内置模拟器（开发/演示/自测）。"""
        return self.connect_tcp("127.0.0.1", port, adapter_name)

    def _connect(self, transport: Transport, adapter_name: Optional[str]) -> ScopeAdapter:
        self.disconnect()
        engine = ScpiEngine(transport)
        self._pending_transport = transport
        engine.before_operation = self._check_cancelled
        try:
            self._check_cancelled()
            engine.connect()
            adapter = create_adapter(engine, adapter_name)
            self._check_cancelled()
        except Exception:
            transport.disconnect()
            raise
        finally:
            self._pending_transport = None
        self.transport = transport
        self.engine = engine
        self.adapter = adapter
        return adapter

    def disconnect(self) -> None:
        if self.engine is not None:
            try:
                self.engine.disconnect()
            finally:
                self.engine = None
        self.transport = None
        self.adapter = None
