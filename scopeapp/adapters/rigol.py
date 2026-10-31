"""Rigol 适配器（DS1000Z / DS2000 / DS4000 等 :WAV:DATA? 风格机型）。"""
from __future__ import annotations

import re
import time
import numpy as np

from ..model import (
    ChannelSettings,
    Coupling,
    TimebaseSettings,
    TriggerSettings,
    Waveform,
)
from ..scpi import parse_ieee_block
from .base import ScopeAdapter
from .registry import adapters_registry


@adapters_registry.register
class RigolAdapter(ScopeAdapter):
    """Rigol 台式示波器适配器。

    标准 DS1000Z 波形路径：BYTE 数据块 + :WAV:PRE? 的十字段参数。
    数据块只有采样值，不含私有 headless 头。
    """

    name = "Rigol (DS1000Z 系列等)"
    idn_pattern = re.compile(r"rigol", re.IGNORECASE)
    channel_count = 4

    def __init__(self, engine):
        super().__init__(engine)
        self._xinc: float | None = None
        self._xorigin: float | None = None
        self._preambles: dict[int, tuple[float, list[float]]] = {}
        match = re.match(r"(?:MSO|DS)\d{3}([24])", self.identity.model if self.identity else "", re.IGNORECASE)
        if match:
            self.channel_count = int(match.group(1))
            self._enabled_cache = {n: True for n in range(1, self.channel_count + 1)}

    # ---------------- 状态 ----------------
    def run(self) -> None:
        mode = self.engine.ask(":TRIG:SWE?").strip().upper()
        if mode.startswith("SING"):
            self.engine.write(":TRIG:SWE AUTO")
        self.engine.write(":RUN")

    def single(self) -> None:
        self.engine.write(":SINGle")

    def force_trigger(self) -> None:
        self.engine.write(":TFORce")

    def autoset(self) -> None:
        self.engine.write(":AUToscale")
        self._xinc = self._xorigin = None
        self._preambles.clear()

    def set_trigger_mode(self, mode: str) -> None:
        self.engine.write(f":TRIG:SWE {mode}")

    def set_trigger_slope(self, slope: str) -> None:
        self.engine.write(f":TRIG:EDGE:SLOP {'POS' if slope == 'RISE' else 'NEG'}")

    def set_channel_coupling(self, number: int, coupling: str) -> None:
        self.engine.write(f":CHAN{number}:COUP {coupling}")
        self._preambles.pop(number, None)

    def stop(self) -> None:
        self.engine.write(":STOP")

    def is_running(self) -> bool:
        return self.engine.ask(":TRIG:STAT?").strip().upper() != "STOP"

    # ---------------- 设置 ----------------
    def read_timebase(self) -> TimebaseSettings:
        return TimebaseSettings(
            scale=self.engine.ask_float(":TIM:SCAL?", 1e-3),
            sample_rate=self.engine.ask_float(":ACQ:SAMP?", 0.0),
            record_length=self.engine.ask_int(":ACQ:MDEP?", 0),
        )

    def read_channel(self, number: int) -> ChannelSettings:
        pre = f":CHAN{number}:"
        coup = self.engine.ask(f"{pre}COUP?").strip().upper()
        bandwidth = self.engine.ask(f"{pre}BWL?").strip().upper()
        return ChannelSettings(
            number=number,
            enabled=self.engine.ask(f"{pre}DISP?").strip().upper() in ("ON", "1"),
            scale=self.engine.ask_float(f"{pre}SCAL?", 1.0),
            offset=self.engine.ask_float(f"{pre}OFFS?", 0.0),
            bandwidth="FULL" if bandwidth in {"OFF", "0"} else "20 MHz" if bandwidth in {"20M", "20MHZ", "ON", "1"} else bandwidth or None,
            coupling=Coupling(coup) if coup in {c.value for c in Coupling} else Coupling.DC,
        )

    def read_trigger(self) -> TriggerSettings:
        return TriggerSettings(
            source=self.engine.ask(":TRIG:EDGE:SOUR?").strip().replace("CHAN", "CH") or "CH1",
            level=self.engine.ask_float(":TRIG:EDGE:LEV?", 0.0),
            slope="RISE" if self.engine.ask(":TRIG:EDGE:SLOP?").strip().upper().startswith("POS") else "FALL",
            mode=self.engine.ask(":TRIG:SWE?").strip().upper(),
        )

    def set_timebase(self, scale: float) -> None:
        self.engine.write(f":TIM:SCAL {scale:g}")
        self._xinc = None
        self._xorigin = None
        self._preambles.clear()

    def set_channel_scale(self, number: int, scale: float) -> None:
        self.engine.write(f":CHAN{number}:SCAL {scale:g}")
        self._preambles.pop(number, None)

    def set_channel_offset(self, number: int, offset: float) -> None:
        self.engine.write(f":CHAN{number}:OFFS {offset:g}")
        self._preambles.pop(number, None)

    def set_channel_enabled(self, number: int, enabled: bool) -> bool:
        self.engine.write(f":CHAN{number}:DISP {'ON' if enabled else 'OFF'}")
        self._enabled_cache[number] = enabled
        return True

    def set_trigger_level(self, level: float) -> None:
        self.engine.write(f":TRIG:EDGE:LEV {level:g}")

    def set_trigger_source(self, source: str) -> None:
        self.engine.write(f":TRIG:EDGE:SOUR {source.replace('CH', 'CHAN')}")

    # ---------------- 波形 ----------------
    def fetch_waveform(self, channel: int) -> Waveform:
        ch_name = f"CHAN{channel}"
        self.engine.write(f":WAV:SOUR {ch_name};:WAV:FORM BYTE;:WAV:MODE NORM")
        cached = self._preambles.get(channel)
        if cached is None or time.monotonic() - cached[0] > 2:
            fields = re.split(r"[,;]", self.engine.ask(":WAV:PRE?").strip())
            if len(fields) != 10:
                raise ValueError("Rigol 波形参数应包含十个字段")
            pre = [float(field) for field in fields]
            if pre[0] != 0 or pre[4] <= 0 or pre[7] <= 0 or not np.all(np.isfinite(pre)):
                raise ValueError("Rigol BYTE 波形参数无效")
            self._preambles[channel] = (time.monotonic(), pre)
        else:
            pre = cached[1]
        self.engine.write(f":WAV:STAR 1;:WAV:STOP {int(pre[2])}")
        payload, _ = parse_ieee_block(self.engine.ask_binary(":WAV:DATA?"))
        raw = np.frombuffer(payload, dtype=np.uint8)
        if raw.size < 2:
            raise ValueError("Rigol 未返回足够的波形采样点")
        y = (raw.astype(np.float64) - pre[8] - pre[9]) * pre[7]
        return Waveform(f"CH{channel}", y, pre[5] - pre[6] * pre[4], pre[4],
                        raw=raw.copy(), source_encoding="UINT8",
                        decode_note="Rigol BYTE + WAV:PRE?; y=(code-YOR-YREF)*YINC")

    # ---------------- 屏幕截图（真实设备屏幕） ----------------
    def capture_screen(self) -> bytes:
        """抓取 Rigol 真实屏幕：:DISPlay:DATA? 返回 24 位 BMP 字节。"""
        resp = self.engine.ask_binary(":DISPlay:DATA?")
        payload, _ = parse_ieee_block(resp)
        if not payload:
            raise RuntimeError("仪器未返回屏幕图像数据")
        return payload
