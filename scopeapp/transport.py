"""传输层抽象：TCP / VISA（USBTMC、串口、LXI）。

所有传输统一接口：write / ask / ask_binary，二进制块（#N<len>...）由上层
scpi 引擎解析，传输层只负责收原始字节流并按行终止符切分。
"""
from __future__ import annotations

import os
import socket
import re
import sys
from abc import ABC, abstractmethod
from typing import Optional
from . import __version__

TERMINATOR = b"\n"
_BLOCK_START_RE = re.compile(rb"#([0-9])([0-9]+)")
_DLL_DIRECTORY_HANDLES = []


def _ensure_libusb() -> None:
    """让 pyusb（pyvisa-py 的 USB 后端）能找到 libusb-1.0.dll。

    - 源码运行：dll 位于 libusb_package 包目录
    - 打包运行：PyInstaller hook 把 dll 复制到 exe 同目录
    必须在任何 pyvisa/pyusb 使用之前执行（本模块被导入即执行）。
    """
    dirs: list[str] = []
    try:
        import libusb_package

        lib = libusb_package.get_library_path()
        if lib:
            dirs.append(os.path.dirname(str(lib)))
    except ImportError:
        pass
    if getattr(sys, "frozen", False):
        # PyInstaller 6.x onedir：依赖在 _internal（sys._MEIPASS），exe 根目录也放一份保险
        for d in (getattr(sys, "_MEIPASS", None),
                  os.path.dirname(sys.executable)):
            if d and d not in dirs:
                dirs.append(d)
    path = os.environ.get("PATH", "")
    for d in dirs:
        if not d:
            continue
        if d not in path.split(os.pathsep):
            path = d + os.pathsep + path
        if hasattr(os, "add_dll_directory"):
            try:
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(d))
            except OSError:
                pass
    if path:
        os.environ["PATH"] = path


_ensure_libusb()


def usb_backend_available() -> bool:
    """pyusb 的 libusb 后端是否可用（决定 pyvisa-py 能否枚举 USBTMC 设备）。"""
    try:
        from usb.backend import libusb1

        return libusb1.get_backend() is not None
    except Exception:  # noqa: BLE001
        return False


def usb_device_claimable(vid: int = 0x1AB1, pid: int = 0x0588) -> str:
    """检测指定 VID/PID 的 USB 设备能否被 libusb claim（驱动兼容性）。

    返回状态文本：'not_found' / 'ok' / 'driver_conflict'（usbtmc.sys 等独占驱动）。
    """
    if not usb_backend_available():
        return "backend_unavailable"
    try:
        import usb.core
        import usb.util

        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            return "not_found"
        dev.set_configuration()
        itf = dev.get_active_configuration()[(0, 0)]
        usb.util.claim_interface(dev, itf)
        usb.util.release_interface(dev, itf)
        return "ok"
    except NotImplementedError:
        return "driver_conflict"  # Windows 上设备被内核驱动（如 usbtmc.sys）占用
    except Exception:  # noqa: BLE001
        return "driver_conflict"


def visa_diagnostic() -> str:
    """返回本机仪器连接环境诊断（GUI 无设备/连接失败时提示用）。"""
    import ctypes.util

    parts = []
    ni = ctypes.util.find_library("visa32") or ctypes.util.find_library("visa64")
    parts.append(f"NI-VISA 后端：{'已安装' if ni else '未安装'}")
    if not usb_backend_available():
        parts.append("libusb 后端：不可用（缺少 libusb-1.0.dll）")
        return "；".join(parts)
    parts.append("libusb 后端：可用")
    claim = usb_device_claimable()
    if claim == "ok":
        parts.append("Rigol 设备：可通过 libusb 访问（WinUSB 驱动）")
    elif claim == "driver_conflict":
        parts.append(
            "Rigol 设备：驱动未安装或不兼容 libusb（设备管理器可能显示感叹号/usbtmc.sys）。"
            "解决：用 Zadig 安装 WinUSB 驱动，或安装 NI-VISA"
        )
    elif claim == "not_found":
        parts.append("Rigol 设备：未检测到（检查 USB 连接/设备电源/驱动是否已安装）")
    try:
        import pyvisa

        parts.append(f"pyvisa：{getattr(pyvisa, '__version__', '已打包')}")
    except ImportError:
        parts.append("pyvisa：未安装")
    return "；".join(parts)


class TransportError(Exception):
    pass


class Transport(ABC):
    """示波器通信传输抽象。"""

    name: str = "base"

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def write(self, cmd: str) -> None: ...

    @abstractmethod
    def read_raw(self) -> bytes: ...

    def ask(self, cmd: str) -> str:
        self.write(cmd)
        raw = self.read_raw()
        return raw.decode("utf-8", errors="replace").strip()

    def ask_binary(self, cmd: str) -> bytes:
        """执行查询并返回原始响应字节（含 #N<len> 块头，若有）。"""
        self.write(cmd)
        return self.read_raw()

    def __enter__(self) -> "Transport":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()


class TcpTransport(Transport):
    """原始 TCP 套接字传输，示波器 LAN 默认端口 5025。"""

    name = "TCP"

    def cancel(self):
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def __init__(self, host: str, port: int = 5025, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buf = bytearray()
        self._after_binary = False

    def connect(self) -> None:
        try:
            self._buf.clear()
            self._after_binary = False
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.settimeout(self.timeout)
        except OSError as e:
            raise TransportError(f"无法连接 {self.host}:{self.port} — {e}") from e

    def disconnect(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        self._buf.clear()

    def write(self, cmd: str) -> None:
        if self._sock is None:
            raise TransportError("未连接")
        data = cmd.encode("utf-8") + TERMINATOR
        try:
            self._sock.sendall(data)
        except OSError as e:
            raise TransportError(f"发送失败: {e}") from e

    def read_raw(self) -> bytes:
        if self._sock is None:
            raise TransportError("未连接")
        while True:
            # A block's optional LF/CRLF may arrive in a later TCP packet.
            # Consume it before recognizing the next query's response.
            if self._after_binary:
                while self._buf[:1] in (b"\r", b"\n"):
                    del self._buf[:1]
                if self._buf:
                    self._after_binary = False
            complete = self._complete_block_len()
            if complete:
                out = bytes(self._buf[:complete])
                del self._buf[:complete]
                self._after_binary = True
                return out
            # 二进制块未收全时，载荷中的 LF 不能当成文本结束符。
            binary_pending = (len(self._buf) >= 2 and self._buf[:1] == b"#"
                              and bytes(self._buf[1:2]).isdigit())
            idx = -1 if binary_pending else self._buf.find(TERMINATOR)
            if idx >= 0:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                return line.rstrip(b"\r")
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout as e:
                raise TransportError("读取超时（设备无响应）") from e
            except OSError as e:
                raise TransportError(f"读取失败: {e}") from e
            if not chunk:
                raise TransportError("连接已关闭")
            self._buf.extend(chunk)

    def _complete_block_len(self) -> int:
        """若缓冲区内存在完整 IEEE 488.2 块则返回其总字节数，否则 0。

        块可能位于缓冲起始处（CURVe?）或某行文本之后（WAVFrm? =
        前导文本 + LF + 块），因此只匹配行首或缓冲起点的块头，避免
        二进制数据里偶然出现的 `#<d><d>` 图案造成误判。
        """
        buf = bytes(self._buf)
        candidates = [0]
        for i, b in enumerate(buf):
            if b == 0x0A and i + 1 < len(buf) and buf[i + 1] == 0x23:  # '\n#'
                candidates.append(i + 1)
        for pos in candidates:
            m = _BLOCK_START_RE.match(buf, pos)
            if not m:
                continue
            ndigits = int(m.group(1))
            length = int(m.group(2)[:ndigits])
            need = pos + 2 + ndigits + length
            if len(buf) >= need:
                return need
        return 0


class VisaTransport(Transport):
    """基于 PyVISA 的传输：覆盖 USBTMC（USB 仪器）、ASRL（串口）、TCPIP（LXI）。

    未安装 pyvisa 或本机无 VISA 后端（NI-VISA / pyvisa-py）时抛 TransportError。
    """

    name = "VISA"

    def __init__(self, resource: str, timeout: float = 5000, backend_spec: str = "", **rm_kwargs):
        self.resource = resource
        self.timeout = timeout
        self._rm_kwargs = rm_kwargs
        self.backend_spec = backend_spec
        self._inst = None
        self._rm = None

    def connect(self) -> None:
        try:
            import pyvisa  # noqa: WPS433
        except ImportError as e:
            raise TransportError("未安装 pyvisa，请 pip install pyvisa pyvisa-py") from e
        try:
            self._rm = pyvisa.ResourceManager(self.backend_spec, **self._rm_kwargs)
            self._inst = self._rm.open_resource(self.resource, open_timeout=int(self.timeout))
            self._inst.timeout = self.timeout
            # USBTMC 使用 EOM；启用文本终止符会截断含 0x0A 的波形。
            self._inst.read_termination = None if self.resource.upper().startswith("USB") else "\n"
            self._inst.write_termination = "\n"
        except Exception as e:  # noqa: BLE001 —— VISA 后端错误五花八门，统一包装
            self.disconnect()
            raise TransportError(f"VISA 打开资源 {self.resource} 失败: {e}") from e

    def disconnect(self) -> None:
        if self._inst is not None:
            try:
                self._inst.close()
            finally:
                self._inst = None
        if self._rm is not None:
            try:
                self._rm.close()
            finally:
                self._rm = None

    def write(self, cmd: str) -> None:
        if self._inst is None:
            raise TransportError("未连接")
        try:
            self._inst.write(cmd)
        except Exception as e:  # noqa: BLE001
            raise TransportError(f"VISA 写入失败: {e}") from e

    def read_raw(self) -> bytes:
        if self._inst is None:
            raise TransportError("未连接")
        try:
            raw = self._inst.read_raw()
        except Exception as e:  # noqa: BLE001
            raise TransportError(f"VISA 读取失败: {e}") from e
        # 0x0A / 0x0D 也可能是最后一个真实采样码，交给块长度解析。
        return raw

    def ask_binary(self, cmd: str) -> bytes:
        if self._inst is None:
            raise TransportError("未连接")
        termination = self._inst.read_termination
        try:
            self._inst.read_termination = None
            return super().ask_binary(cmd)
        finally:
            self._inst.read_termination = termination


    def ask_binary_with_timeout(self, cmd: str, timeout_ms: int) -> bytes:
        if self._inst is None:
            raise TransportError("未连接")
        previous = self._inst.timeout
        try:
            self._inst.timeout = timeout_ms
            return self.ask_binary(cmd)
        finally:
            self._inst.timeout = previous


def scan_visa_resources(backend_spec: str = "") -> dict:
    """扫描并返回所用 VISA 库；错误必须报告，不能伪装成空列表。"""
    try:
        import pyvisa  # noqa: WPS433
    except ImportError as error:
        raise TransportError("缺少 PyVISA 模块，请保留完整的软件 _internal 目录") from error
    manager = None
    try:
        # ResourceManager 并不实现上下文管理协议，不能使用 with。
        manager = pyvisa.ResourceManager(backend_spec)
        resources = list(manager.list_resources())
        return {"scopehost_version": __version__,
                "runtime": "packaged" if getattr(sys, "frozen", False) else "python",
                "resources": resources, "backend_requested": backend_spec or "auto",
                "backend": str(manager.visalib), "pyvisa_version": pyvisa.__version__,
                "python_bits": 64 if sys.maxsize > 2**32 else 32,
                "usb_backend_available": usb_backend_available(),
                "usb_resources": [r for r in resources if r.upper().startswith("USB")],
                "serial_resources": [r for r in resources if r.upper().startswith("ASRL")]}
    except Exception as error:
        raise TransportError(f"VISA 扫描失败（{backend_spec or '自动后端'}）：{type(error).__name__}: {error}") from error
    finally:
        if manager is not None:
            manager.close()


def list_visa_resources(backend_spec: str = "") -> list[str]:
    return scan_visa_resources(backend_spec)["resources"]
