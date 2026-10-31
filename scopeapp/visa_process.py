"""隔离 VISA 原生驱动：驱动卡住时可终止本软件的子进程，界面不等待驱动。"""
from __future__ import annotations

import multiprocessing as mp
import threading
import time

from .transport import Transport, TransportError


def _visa_server(pipe):
    from .transport import VisaTransport, scan_visa_resources

    transport = None
    try:
        while True:
            operation, args = pipe.recv()
            try:
                if operation == "scan":
                    result = scan_visa_resources(*args)
                elif operation == "connect":
                    transport = VisaTransport(*args)
                    result = transport.connect()
                elif operation == "close":
                    if transport:
                        transport.disconnect()
                    pipe.send((True, None))
                    return
                else:
                    if transport is None:
                        raise TransportError("VISA 未连接")
                    result = getattr(transport, operation)(*args)
                pipe.send((True, result))
            except Exception as error:
                pipe.send((False, str(error)))
    except (EOFError, OSError):
        pass
    finally:
        if transport:
            transport.disconnect()
        pipe.close()


class VisaProcessTransport(Transport):
    name = "VISA"

    def __init__(self, resource: str = "", timeout: int = 5000, backend_spec: str = ""):
        self.resource = resource
        self.timeout = timeout
        self.backend_spec = backend_spec
        self._process = self._pipe = None
        self._cancel = threading.Event()

    def _start(self):
        context = mp.get_context("spawn")
        parent, child = context.Pipe()
        self._pipe = parent
        self._process = context.Process(target=_visa_server, args=(child,), daemon=True)
        try:
            self._process.start()
        except Exception:
            self._abort()
            raise
        finally:
            child.close()

    def _abort(self):
        if self._pipe:
            self._pipe.close()
            self._pipe = None
        if self._process:
            if self._process.is_alive():
                self._process.terminate()
            if self._process.pid is not None:
                self._process.join(0.2)
            if not self._process.is_alive():
                self._process.close()
            self._process = None

    def cancel(self):
        self._cancel.set()

    def _rpc(self, operation, *args, deadline_ms=None):
        if self._cancel.is_set():
            self._abort()
            raise TransportError("VISA 操作已取消")
        if not self._pipe or not self._process or not self._process.is_alive():
            raise TransportError("VISA 通信进程已退出，请重新连接设备")
        deadline = time.monotonic() + (deadline_ms or self.timeout + 1500) / 1000
        try:
            self._pipe.send((operation, args))
            while not self._pipe.poll(0.05):
                if self._cancel.is_set():
                    raise TransportError("VISA 操作已取消")
                if time.monotonic() >= deadline:
                    raise TransportError(f"VISA {operation} 超时，已释放通信进程；请检查驱动和设备后重新连接")
                if not self._process.is_alive():
                    raise TransportError("VISA 驱动进程异常退出，请重新连接")
            success, result = self._pipe.recv()
        except (EOFError, OSError, TransportError) as error:
            self._abort()
            if isinstance(error, TransportError):
                raise
            raise TransportError(f"VISA 进程通信失败：{error}") from error
        if not success:
            # 超时后的 VISA 会话可能残留未读响应；终止会话，避免下一条查询串包。
            self._abort()
            raise TransportError(result)
        return result

    def connect(self):
        self._start()
        self._rpc("connect", self.resource, self.timeout, self.backend_spec)

    def disconnect(self):
        if self._process:
            try:
                self._rpc("close", deadline_ms=300)
            except TransportError:
                pass
            finally:
                self._abort()

    def write(self, cmd: str):
        self._rpc("write", cmd)

    def read_raw(self) -> bytes:
        return self._rpc("read_raw")

    def ask(self, cmd: str) -> str:
        return self._rpc("ask", cmd)

    def ask_binary(self, cmd: str) -> bytes:
        return self._rpc("ask_binary", cmd)

    def ask_binary_with_timeout(self, cmd: str, timeout_ms: int) -> bytes:
        return self._rpc("ask_binary_with_timeout", cmd, timeout_ms, deadline_ms=timeout_ms + 1500)

    def scan(self) -> list[str]:
        return self.scan_report()["resources"]

    def scan_report(self) -> dict:
        self._start()
        try:
            return self._rpc("scan", self.backend_spec, deadline_ms=8000)
        finally:
            self.disconnect()
