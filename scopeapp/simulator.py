"""内置 SCPI 模拟示波器（模拟 Tektronix MSO44B 的子集行为）。

无需真实硬件即可端到端开发与自测：上位机通过 TCP 连到本模拟器，
行为与手册一致：
  - *IDN? -> TEKTRONIX,MSO44B,...
  - :WFMOutpre? -> "KEY value;KEY value;..." 键值对
  - :CURVe? -> #N<len> 原始二进制块（BYT_Nr 1=int8 / 2=int16，MSB）
  - :WAVFrm? -> 前导文本 + LF + 二进制块

自定义命令：
  SIM:WAVE CHn,TYPE,freqHz,ampVpp        （sine/square/ramp/chirp/noise）
  SIM:NOISE CHn,levelVpp

用法：
    python -m scopeapp.simulator --port 5025
"""
from __future__ import annotations

import argparse
import math
import random
import re
import socketserver
import struct
import threading
import time
from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------- 模型

def _render_screen(scope: "_SimScope") -> np.ndarray:
    """把模拟器状态渲染成一张"示波器屏幕"RGB 图像。"""
    from .screen_render import render_scope_screen
    from .model import ChannelSettings, TimebaseSettings, Waveform

    wf = {}
    for i in range(1, 5):
        ch_name = f"CH{i}"
        if not scope.channels[ch_name]["display"]:
            continue
        v = scope.wave_volts(ch_name)
        n = scope.rec_len
        wf[ch_name] = Waveform(channel=ch_name, y=v, x_start=0.0,
                               x_incr=scope.xincr, points=n)
    channels = {
        f"CH{i}": ChannelSettings(
            number=i,
            enabled=bool(scope.channels[f"CH{i}"]["display"]),
            scale=scope.channels[f"CH{i}"]["scale"],
            offset=scope.channels[f"CH{i}"]["offset"],
        )
        for i in range(1, 5)
    }
    tb = TimebaseSettings(scale=scope.hscale, sample_rate=scope.sample_rate)
    return render_scope_screen(wf, channels, tb)


def _png_bytes(img: np.ndarray) -> bytes:
    from .screen_render import png_bytes

    return png_bytes(img)


def _bmp_bytes(img: np.ndarray) -> bytes:
    from .screen_render import bmp_bytes

    return bmp_bytes(img)

@dataclass
class _Signal:
    kind: str = "sine"
    freq: float = 1_000.0
    amp: float = 2.0        # Vpp
    noise: float = 0.02     # Vpp


@dataclass
class _SimScope:
    """模拟器的示波器状态。"""

    serial: str = "SIM00042"
    fw: str = "SIM-FW-1.0"
    header: int = 0                 # HEADER ON/OFF（仅影响 WFMOutpre? 首 token 前缀）
    running: bool = True
    stop_after: str = "RUNSTOP"
    armed_at: float = 0.0
    hscale: float = 1e-3            # s/div
    rec_len: int = 10_000
    data_source: str = "CH1"
    data_start: int = 1
    data_stop: int = 10_000
    data_resample: int = 1
    encdg: str = "BINARY"
    byt_nr: int = 2                 # 1 或 2
    bn_fmt: str = "RI"
    byt_or: str = "MSB"
    channels: dict = field(default_factory=lambda: {
        f"CH{i}": {
            "scale": 1.0,
            "offset": 0.0,
            "coupling": "DC",
            "display": 1,
            "signal": _Signal(),
        }
        for i in range(1, 5)
    })
    trig_level: float = 0.0
    trig_source: str = "CH1"
    trig_mode: str = "AUTO"
    trig_slope: str = "RISE"
    errors: list = field(default_factory=list)
    files: dict = field(default_factory=dict)   # 虚拟文件系统 {路径: 字节}

    def acquisition_state(self) -> str:
        if self.running and self.stop_after == "SEQUENCE" and time.monotonic() - self.armed_at > 0.15:
            values = self.wave_volts(self.trig_source)
            if self.trig_slope == "FALL":
                crossing = (values[:-1] > self.trig_level) & (values[1:] <= self.trig_level)
            else:
                crossing = (values[:-1] < self.trig_level) & (values[1:] >= self.trig_level)
            if self.channels[self.trig_source]["display"] and np.any(crossing):
                self.running = False
        return "1" if self.running else "0"

    # ---------------- 派生参数 ----------------
    @property
    def window(self) -> float:
        return 10.0 * self.hscale

    @property
    def xincr(self) -> float:
        return self.window / self.rec_len

    @property
    def sample_rate(self) -> float:
        return 1.0 / self.xincr

    def y_incr(self, ch: str) -> float:
        # Tektronix：25 digitizing levels / div
        return self.channels[ch]["scale"] / 25.0

    # ---------------- 信号生成 ----------------
    def wave_volts(self, ch: str) -> np.ndarray:
        sig = self.channels[ch]["signal"]
        n = self.rec_len
        t = np.arange(n) * self.xincr
        if sig.kind == "square":
            v = np.where(np.sin(2 * math.pi * sig.freq * t) >= 0, 1.0, -1.0) * sig.amp / 2
        elif sig.kind == "ramp":
            v = (2 * (sig.freq * t % 1.0) - 1.0) * sig.amp / 2
        elif sig.kind == "chirp":
            f1, f2 = sig.freq, sig.freq * 20
            phase = 2 * math.pi * (f1 * t + (f2 - f1) * t**2 / (2 * t[-1] if t[-1] else 1))
            v = np.sin(phase) * sig.amp / 2
        elif sig.kind == "noise":
            v = np.random.default_rng(42).uniform(-1, 1, n) * sig.amp / 2
        else:  # sine
            v = np.sin(2 * math.pi * sig.freq * t) * sig.amp / 2
        if sig.noise > 0:
            v = v + np.random.default_rng(sum(ch.encode()) & 0xFFFF).uniform(
                -sig.noise / 2, sig.noise / 2, n
            )
        if self.channels[ch]["coupling"] == "GND":
            v = np.zeros(n)
        elif self.channels[ch]["coupling"] == "AC":
            v -= np.mean(v)
        return v

    def curve_bytes(self, ch: str) -> bytes:
        """生成 CURVe? 的块内数据（无内嵌头，与 MSO44B 一致）。"""
        v = self.wave_volts(ch)
        offset = self.channels[ch]["offset"]
        incr = self.y_incr(ch)
        codes = np.clip(np.rint((v - offset) / incr), -32768, 32767)
        n = self.rec_len
        if self.byt_nr == 1:
            codes = np.clip(codes, -128, 127).astype(np.int8)
            return codes.astype(np.int8, copy=False).tobytes()
        return codes.astype(np.int16).astype(">i2").tobytes() if self.byt_or == "MSB" \
            else codes.astype(np.int16).astype("<i2").tobytes()

    def wfmoutpre(self, ch: str) -> str:
        c = self.channels[ch]
        pts = self.data_stop - self.data_start + 1
        pts = math.ceil(max(1, min(pts, self.rec_len)) / self.data_resample)
        wfid = f'"Ch{ch[-1]}, {c["coupling"]} coupling, {c["scale"]:g}V/div, {self.hscale:g}s/div, {pts} points, Sample mode"'
        fields = [
            ("BYT_NR", self.byt_nr),
            ("BIT_NR", self.byt_nr * 8),
            ("ENCDG", self.encdg),
            ("BN_FMT", self.bn_fmt),
            ("BYT_OR", self.byt_or),
            ("WFID", wfid),
            ("NR_PT", pts),
            ("PT_FMT", "Y"),
            ("PT_ORDER", "LINEAR"),
            ("XUNIT", '"s"'),
            ("XINCR", f"{self.xincr * self.data_resample:.6E}"),
            ("XZERO", "0.0E+0"),
            ("PT_OFF", (self.rec_len / 2 - self.data_start + 1) / self.data_resample),
            ("YUNIT", '"V"'),
            ("YMULT", f"{self.y_incr(ch):.6E}"),
            ("YOFF", "0.0E+0"),
            ("YZERO", f"{c['offset']:.6E}"),
        ]
        body = ";".join(f"{k} {v}" for k, v in fields)
        if self.header:
            return ":WFMOUTPRE:" + body
        return ";".join(str(value) for _, value in fields)

    def curve_block(self) -> bytes:
        """CURVe? 完整响应：IEEE 块头 + 数据（按 DATa:STARt/STOP 切片）。"""
        ch = self._resolve_channel(self.data_source)
        data = self.curve_bytes(ch)
        width = self.byt_nr
        lo = (self.data_start - 1) * width
        hi = self.data_stop * width
        data = np.frombuffer(data[lo:hi], dtype=f"V{width}")[::self.data_resample].tobytes()
        n = len(str(len(data)))
        return b"#" + str(n).encode() + str(len(data)).encode() + data

    @staticmethod
    def _resolve_channel(src: str) -> str:
        m = re.search(r"CH(?:AN)?(\d)", src.upper())
        return f"CH{m.group(1)}" if m else "CH1"


# ---------------------------------------------------------------- 命令处理

def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:  # noqa: D102
        scope = self.server.scope  # type: ignore[attr-defined]
        lock = self.server.lock  # type: ignore[attr-defined]
        while True:
            try:
                line = self.rfile.readline()
            except Exception:  # noqa: BLE001
                break
            if not line:
                break
            cmd = line.decode("utf-8", errors="replace").strip()
            if not cmd:
                continue
            try:
                with lock:
                    resp = self._process(scope, cmd)
            except Exception as e:  # noqa: BLE001 —— 模拟器不因单命令崩溃
                resp = f'-113,"Undefined header: {e}"'
            if resp is not None:
                try:
                    if isinstance(resp, bytes):
                        self.wfile.write(resp + b"\n")
                    else:
                        self.wfile.write(resp.encode("utf-8") + b"\n")
                    self.wfile.flush()
                except Exception:  # noqa: BLE001
                    break

    @staticmethod
    def _process(scope: _SimScope, cmd: str) -> str | bytes | None:
        """处理一条命令（含 ';' 复合），返回响应文本/字节或 None。"""
        parts = [p.strip() for p in cmd.split(";") if p.strip()]
        responses = []
        for part in parts:
            r = _dispatch(scope, part)
            if r is not None:
                responses.append(r)
        if not responses:
            return None
        # 任一响应为二进制时，整体按二进制返回（复合查询罕见，取最后一段块）
        if any(isinstance(r, bytes) for r in responses):
            return responses[-1] if isinstance(responses[-1], bytes) else responses[-1].encode()
        return ";".join(str(r) for r in responses)


def _dispatch(scope: _SimScope, cmd: str) -> str | None:
    """单条命令分发。query 返回文本，write 返回 None。"""
    m = re.match(r"^:?([A-Za-z0-9*:?]+)\s*(.*)$", cmd)
    if not m:
        return None
    head = m.group(1).upper()
    args = m.group(2).strip()

    # ---- 系统 ----
    if head == "*IDN?":
        return f"TEKTRONIX,MSO44B,{scope.serial},{scope.fw}"
    if head in ("*RST", "*CLS"):
        scope.running = True
        scope.errors.clear()
        return None
    if head == "*ESR?":
        return "0"
    if head == "*OPC?":
        return "1"
    if head == "*STB?":
        return "0"
    if head == "*TST?":
        return "0"
    if head in ("SYST:ERR", "SYSTEM:ERR") or head == "SYST:ERR?":
        if scope.errors:
            return scope.errors.pop(0)
        return '0,"No error"'
    if head == "HEADER" or head == "HEADER?":
        if args:
            scope.header = 1 if args.upper() in ("1", "ON") else 0
            return None
        return "1" if scope.header else "0"
    if head in ("VERBOSE", "VERBOSE?"):
        return "1" if head.endswith("?") else None
    if head in ("SEQUENCE", "SEQUENCE?"):
        return "0"

    # ---- 采集 ----
    if head == "ACQ:STATE" or head == "ACQUIRE:STATE":
        if args:
            scope.running = args.upper() in ("RUN", "1", "ON")
            scope.armed_at = time.monotonic()
            return None
        return "1" if scope.running else "0"
    if head == "ACQ:STATE?" or head == "ACQUIRE:STATE?":
        return scope.acquisition_state()
    if head in ("ACQ:NUMACQ?", "ACQUIRE:NUMACQ?"):
        return "0"
    if head in ("ACQ:STOPAFTER", "ACQUIRE:STOPAFTER"):
        scope.stop_after = "SEQUENCE" if args.upper().startswith("SEQ") else "RUNSTOP"
        return None
    if head in ("ACQ:STOPAFTER?", "ACQUIRE:STOPAFTER?"):
        return scope.stop_after
    if head in ("TRIGGER", "TRIG") and args.upper() == "FORCE":
        if scope.stop_after == "SEQUENCE":
            scope.running = False
        return None
    if head == "AUTOSET":
        sig = scope.channels[scope.trig_source]["signal"]
        scope.hscale = 0.4 / max(1, sig.freq)
        scope.trig_level = 0
        for ch in scope.channels.values():
            ch["scale"] = max(0.001, ch["signal"].amp / 6)
            ch["offset"] = 0
        return None
    if head in ("ACQ:MODE?", "ACQUIRE:MODE?"):
        return "SAMPLE"

    # ---- 时基 ----
    if head in ("HOR:SCAL", "HOR:SCALE", "HORIZONTAL:SCAL", "HORIZONTAL:SCALE"):
        if args:
            scope.hscale = float(args)
            scope.data_stop = scope.rec_len
            return None
        return _fmt(scope.hscale)
    if head in ("HOR:SCAL?", "HOR:SCALE?", "HORIZONTAL:SCAL?", "HORIZONTAL:SCALE?"):
        return _fmt(scope.hscale)
    if head in ("HOR:SAMPLERATE?", "HORIZONTAL:SAMPLERATE?"):
        return _fmt(scope.sample_rate)
    if head in ("HOR:RECORDLENGTH?", "HORIZONTAL:RECORDLENGTH?"):
        return str(scope.rec_len)
    if head in ("HOR:DIVISIONS?", "HORIZONTAL:DIVISIONS?"):
        return "10"
    if head in ("HOR:DELAY:MODE?", "HORIZONTAL:DELAY:MODE?"):
        return "0"
    if head in ("HOR:DELAY:TIME?", "HORIZONTAL:DELAY:TIME?"):
        return "0"
    if head in ("HOR:POSITION?", "HORIZONTAL:POSITION?"):
        return "50"

    display = re.fullmatch(r"DISPLAY:GLOBAL:(CH\d):STATE(\?)?", head)
    if display:
        ch = scope.channels[display[1]]
        if display[2]:
            return str(ch["display"])
        ch["display"] = int(args.upper() in ("ON", "1"))
        return None

    # ---- 通道 ----
    chm = re.match(r"CH(\d):(.+)", head)
    if chm:
        ch = f"CH{chm.group(1)}"
        sub_raw = chm.group(2)
        is_q = sub_raw.endswith("?")
        sub = sub_raw[:-1] if is_q else sub_raw
        if sub in ("SCAL", "SCALE"):
            if args:
                scope.channels[ch]["scale"] = float(args)
                return None
            return _fmt(scope.channels[ch]["scale"]) if is_q else None
        if sub in ("OFFS", "OFFSET"):
            if args:
                scope.channels[ch]["offset"] = float(args)
                return None
            return _fmt(scope.channels[ch]["offset"]) if is_q else None
        if sub in ("POSITION", "POS"):
            return "0" if is_q else None
        if sub in ("BANDWIDTH", "BWL", "BWLIMIT"):
            if args:
                scope.channels[ch]["bandwidth"] = args.upper()
                return None
            return str(scope.channels[ch].get("bandwidth", "FULL")) if is_q else None
        if sub == "COUPLING":
            if args:
                scope.channels[ch]["coupling"] = args.upper()
                return None
            return scope.channels[ch]["coupling"] if is_q else None
        if sub == "DISPLAY":
            if args:
                scope.channels[ch]["display"] = 1 if args.upper() in ("1", "ON") else 0
                return None
            return "1" if scope.channels[ch]["display"] else "0" if is_q else None
        return None

    # ---- 触发 ----
    if head in ("TRIG:A:MODE", "TRIGGER:A:MODE"):
        scope.trig_mode = args.upper()
        return None
    if head in ("TRIG:A:MODE?", "TRIGGER:A:MODE?"):
        return scope.trig_mode
    if head in ("TRIG:A:EDGE:SLOPE", "TRIGGER:A:EDGE:SLOPE"):
        scope.trig_slope = args.upper()
        return None
    if head in ("TRIG:A:EDGE:SLOPE?", "TRIGGER:A:EDGE:SLOPE?"):
        return scope.trig_slope
    if head == "TRIG:A:LEVEL" or head == "TRIGGER:A:LEVEL":
        if args:
            scope.trig_level = float(args)
            return None
        return _fmt(scope.trig_level)
    if re.fullmatch(r"(?:TRIG|TRIGGER):A:LEVEL:CH\d\??", head):
        if head.endswith("?"):
            return _fmt(scope.trig_level)
        scope.trig_level = float(args)
        return None
    if head in ("TRIG:A:LEVEL?", "TRIGGER:A:LEVEL?"):
        return _fmt(scope.trig_level)
    if head == "TRIG:A:EDGE:SOURCE" or head == "TRIGGER:A:EDGE:SOURCE":
        if args:
            scope.trig_source = _SimScope._resolve_channel(args)
            return None
        return scope.trig_source
    if head in ("TRIG:A:EDGE:SOURCE?", "TRIGGER:A:EDGE:SOURCE?"):
        return scope.trig_source

    # ---- 波形传输 ----
    if head in ("DATA:RESAMPLE", "DAT:RESAMPLE"):
        scope.data_resample = max(1, int(args))
        return None
    if head == "DATA:SOURCE" or head == "DAT:SOURCE":
        if args:
            scope.data_source = _SimScope._resolve_channel(args)
            return None
        return scope.data_source
    if head in ("DATA:SOURCE?", "DAT:SOURCE?"):
        return scope.data_source
    if head in ("DATA:SOURCE:AVAILABLE?", "DAT:SOU:AVAIL?", "DATA:SOU:AVAIL?"):
        # 离线模拟器的已启用信号立即可用；真机需等采集完成。
        sources = [name for name, channel in scope.channels.items() if channel["display"]]
        return ",".join(sources) if sources else "NONE"
    if head in ("DATA:START", "DAT:START"):
        if args:
            scope.data_start = max(1, int(float(args)))
            return None
        return str(scope.data_start)
    if head in ("DATA:START?", "DAT:START?"):
        return str(scope.data_start)
    if head in ("DATA:STOP", "DAT:STOP"):
        if args:
            scope.data_stop = max(1, min(int(float(args)), scope.rec_len))
            return None
        return str(scope.data_stop)
    if head in ("DATA:STOP?", "DAT:STOP?"):
        return str(scope.data_stop)
    if head in ("DATA:WIDTH?", "DAT:WIDTH?"):
        return str(scope.byt_nr)
    if head in ("DATA:ENC", "DAT:ENC"):
        if args:
            scope.encdg = "BINARY" if args.upper().startswith("BIN") else "ASCII"
            return None
        return scope.encdg
    if head in ("DATA:ENC?", "DAT:ENC?"):
        return scope.encdg

    # WFMOutpre 各子命令
    wm = re.match(r"WFMOUTPRE:(\w+)\??$", head)
    if wm:
        key = wm.group(1)
        mapping = {
            "ENCDG": lambda: scope.encdg,
            "BYT_NR": lambda: str(scope.byt_nr),
            "BYT_NR?": lambda: str(scope.byt_nr),
            "BN_FMT": lambda: scope.bn_fmt,
            "BYT_OR": lambda: scope.byt_or,
            "NR_PT?": lambda: str(scope.data_stop - scope.data_start + 1),
        }
        if args:
            up = args.upper()
            if key == "ENCDG":
                scope.encdg = "BINARY" if up.startswith("BIN") else "ASCII"
            elif key in ("BYT_NR", "BYT_NR?"):
                scope.byt_nr = 1 if int(float(args)) <= 1 else 2
            elif key == "BN_FMT":
                scope.bn_fmt = up
            elif key == "BYT_OR":
                scope.byt_or = up
            return None
        fn = mapping.get(key)
        return fn() if fn else None

    if head == "WFMOUTPRE?":
        return scope.wfmoutpre(scope.data_source)
    if head == "CURVE?" or head == "CURV?":
        return scope.curve_block()
    if head == "WAVFRM?" or head == "WAVF?":
        return scope.wfmoutpre(scope.data_source).encode("utf-8") + b"\n" + scope.curve_block()

    # ---- 屏幕抓取（模拟真实示波器 SAVE:IMAGE → FILESystem:READFile）----
    if head == "SAVE:IMAGE":
        path = _strip_quotes(args)
        scope.files[path] = _png_bytes(_render_screen(scope))
        return None
    if head == "FILESYSTEM:READFILE":
        path = _strip_quotes(args)
        data = scope.files.get(path)
        if data is None:
            scope.errors.append('-110,"Command error: file not found"')
            return None
        n = len(str(len(data)))
        return b"#" + str(n).encode() + str(len(data)).encode() + data
    if head == "FILESYSTEM:DELETE":
        path = _strip_quotes(args)
        scope.files.pop(path, None)
        return None
    if head == "DISPLAY:DATA?":
        return _bmp_bytes(_render_screen(scope))
    if head == "FILESYSTEM:CWD?":
        return '"C:/"'

    # ---- 自定义：信号注入 ----
    if head == "SIM:WAVE" or head == "SIM:WAVE?":
        if not args:
            return "sine,1000,2"
        parts = [p.strip() for p in args.split(",")]
        ch = _SimScope._resolve_channel(parts[0])
        sig = scope.channels[ch]["signal"]
        if len(parts) >= 4:
            sig.kind = parts[1].lower()
            sig.freq = float(parts[2])
            sig.amp = float(parts[3])
        elif len(parts) == 3:
            sig.kind = parts[1].lower()
            sig.freq = float(parts[2])
        return None
    if head == "SIM:NOISE":
        parts = [p.strip() for p in args.split(",")]
        ch = _SimScope._resolve_channel(parts[0])
        scope.channels[ch]["signal"].noise = float(parts[1]) if len(parts) > 1 else 0.02
        return None

    # ---- 未知命令 ----
    if cmd_has_question(head):
        scope.errors.append('-113,"Undefined header"')
        return '-113,"Undefined header"'
    return None


def cmd_has_question(head: str) -> bool:
    return head.endswith("?")


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


# ---------------------------------------------------------------- 服务器

class _ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, scope: _SimScope):
        super().__init__(addr, _Handler)
        self.scope = scope
        self.lock = threading.Lock()


class SimulatorServer:
    """可编程启动/停止的模拟示波器服务器。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 5025):
        self.host = host
        self.port = port
        self.scope = _SimScope()
        self._server: _ThreadedServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def start(self) -> None:
        self._server = _ThreadedServer((self.host, self.port), self.scope)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> "SimulatorServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="SCPI 模拟示波器（MSO44B 行为）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5025)
    args = parser.parse_args()
    with SimulatorServer(args.host, args.port) as srv:
        print(f"模拟示波器运行中: {srv.address}（Ctrl+C 退出）")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("已停止")


if __name__ == "__main__":
    main()
