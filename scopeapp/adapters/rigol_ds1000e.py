"""Rigol DS1000E/D 系列适配器（DS1052E / DS1102E / DS1152E 等）。

与 DS1000Z 系列差异巨大（重要）：
  - 无 :WAV:SOUR / :WAV:FORM / :WAV:MODE —— 波形用 :WAV:DATA? <source> 直接读取
  - 无 :DISPlay:DATA?（不能远程回传屏幕图像）—— 抓屏用数据重建
  - 无 :SYST:ERR? / :ACQ:STATE? —— **未知命令会挂起直到超时**，
    因此本适配器只发经过实测验证的命令，绝不调用 check_error()
  - 波形数据：无符号字节（BYTE），0-255
    电压换算（DS1152E 校准方波实测）：V = (125-code)*ChanScale/25 - ChanOffset
  - 屏幕波形为 12 格 × 8 格，600 点覆盖 12*TimeScale
  - :WAV:POINts:MODE {NORMal|MAXimum|RAW} 控制点数：NORM=600(屏幕)/RAW=8k
"""
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
class RigolDs1000eAdapter(ScopeAdapter):
    """Rigol DS1000E/D 系列适配器（老机型专用命令集）。"""

    name = "Rigol DS1000E/D (DS1152E 等)"
    idn_pattern = re.compile(r"rigol.*ds1\d{3}[ed]", re.IGNORECASE)
    channel_count = 2
    settings_before_waveform = True

    def __init__(self, engine):
        super().__init__(engine)
        # DS1000E 不支持 :SYST:ERR?（查询会挂起 4s），禁用错误查询
        self.error_query_supported = False
        self._ch_cache: dict[int, ChannelSettings] = {}
        self._tb: TimebaseSettings | None = None
        self._trig: TriggerSettings | None = None
        self._settle_until = 0.0
        self._waveform_mode_ready = False

    def _write(self, command: str, settle: bool = False) -> None:
        # 老固件连续快速写入会错误应用参数。逐条发送并留出处理时间。
        self.engine.write(command)
        time.sleep(0.1)
        if settle:
            self._settle_until = time.monotonic() + 0.5

    def _wait_settled(self) -> None:
        remaining = self._settle_until - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    @staticmethod
    def _number(value: float) -> str:
        return f"{value:.12f}".rstrip("0").rstrip(".") or "0"

    # ---------------- 状态 ----------------
    def run(self) -> None:
        mode = self.engine.ask(":TRIG:EDGE:SWE?").strip().upper()
        if mode.startswith("SING"):
            self._write(":TRIG:EDGE:SWE AUTO")
        self._write(":RUN")

    def stop(self) -> None:
        self._write(":STOP")

    def is_running(self) -> bool:
        # DS1000E 编程手册的 :TRIGger:STATus?，不要发送 :ACQ:STATE?。
        status = self.engine.ask(":TRIG:STAT?").strip().upper()
        if status not in {"STOP", "RUN", "WAIT", "AUTO", "T'D"}:
            raise RuntimeError(f"无效的触发状态：{status!r}")
        return status != "STOP"

    def single(self) -> None:
        for command in (":TRIG:MODE EDGE", ":TRIG:EDGE:SWE SINGle", ":RUN"):
            self._write(command)

    def force_trigger(self) -> None:
        self._write(":FORCetrig")

    def autoset(self) -> None:
        self._write(":AUTO", settle=True)
        self._settle_until = time.monotonic() + 2.0
        self._ch_cache.clear()
        self._tb = self._trig = None

    def set_trigger_mode(self, mode: str) -> None:
        self._write(":TRIG:MODE EDGE")
        self._write(f":TRIG:EDGE:SWE {mode}", settle=True)

    def set_trigger_slope(self, slope: str) -> None:
        self._write(f":TRIG:EDGE:SLOP {'POS' if slope == 'RISE' else 'NEG'}", settle=True)

    def set_channel_coupling(self, number: int, coupling: str) -> None:
        self._write(f":CHAN{number}:COUP {coupling}", settle=True)
        self._ch_cache.pop(number, None)

    # ---------------- 设置 ----------------
    def read_timebase(self) -> TimebaseSettings:
        self._wait_settled()
        self._tb = TimebaseSettings(
            scale=float(self.engine.ask(":TIM:SCAL?")),
            position=float(self.engine.ask(":TIM:OFFS?")),
            sample_rate=float(self.engine.ask(":ACQ:SAMP?")),
            record_length=600,
            horizontal_divisions=12,
        )
        return self._tb

    def read_channel(self, number: int) -> ChannelSettings:
        if number not in (1, 2):
            raise ValueError("DS1000E 只有 CH1、CH2")
        self._wait_settled()
        pre = f":CHAN{number}:"
        coup = self.engine.ask(f"{pre}COUP?").strip().upper()
        bandwidth = self.engine.ask(f"{pre}BWL?").strip().upper()
        if bandwidth not in {"ON", "OFF", "1", "0"}:
            raise RuntimeError(f"CH{number} 带宽限制读回无效：{bandwidth!r}")
        ch = ChannelSettings(
            number=number,
            enabled=self.engine.ask(f"{pre}DISP?").strip().upper() in {"1", "ON"},
            scale=float(self.engine.ask(f"{pre}SCAL?")),
            offset=float(self.engine.ask(f"{pre}OFFS?")),
            offset_polarity=1,
            bandwidth="20 MHz" if bandwidth in {"ON", "1"} else "FULL",
            coupling=Coupling(coup) if coup in {c.value for c in Coupling} else Coupling.DC,
        )
        self._ch_cache[number] = ch
        return ch

    def read_trigger(self) -> TriggerSettings:
        self._wait_settled()
        self._trig = TriggerSettings(
            source=self.engine.ask(":TRIG:EDGE:SOUR?").strip().replace("CHAN", "CH") or "CH1",
            level=float(self.engine.ask(":TRIG:EDGE:LEV?")),
            slope="RISE" if self.engine.ask(":TRIG:EDGE:SLOP?").strip().upper().startswith("POS") else "FALL",
            mode=self.engine.ask(":TRIG:EDGE:SWE?").strip().upper(),
        )
        return self._trig

    def set_timebase(self, scale: float) -> None:
        # DS1000E 只接受 1-2-5 档位，无效档位会被忽略（读回纠正）
        self._write(f":TIM:SCAL {self._number(scale)}", settle=True)
        self._tb = None

    def set_channel_scale(self, number: int, scale: float) -> None:
        self._write(f":CHAN{number}:SCAL {self._number(scale)}", settle=True)
        self._ch_cache.pop(number, None)

    def set_channel_offset(self, number: int, offset: float) -> None:
        self._write(f":CHAN{number}:OFFS {self._number(offset)}", settle=True)
        self._ch_cache.pop(number, None)

    def set_channel_enabled(self, number: int, enabled: bool) -> bool:
        """切换通道显示，写后读回验证是否生效。

        DS1000E 固件 00.04 存在已知限制：通道显示一旦关闭（DISP=0），
        SCPI 无法再打开（DISP ON/OFF、AUT 均无效，命令被接受但无效果）。
        返回 True=生效，False=固件限制未生效（需在示波器面板上操作）。
        """
        target = "1" if enabled else "0"
        self._write(f":CHAN{number}:DISP {target}", settle=True)
        self._wait_settled()
        actual = self.engine.ask(f":CHAN{number}:DISP?").strip()
        applied = actual == target
        self._enabled_cache[number] = actual == "1"
        if applied:
            self._ch_cache.pop(number, None)
        return applied

    def set_trigger_level(self, level: float) -> None:
        self._write(f":TRIG:EDGE:LEV {self._number(level)}", settle=True)

    def set_trigger_source(self, source: str) -> None:
        self._write(f":TRIG:EDGE:SOUR {source}", settle=True)

    # ---------------- 波形 ----------------
    def fetch_waveform(self, channel: int) -> Waveform:
        if not self._waveform_mode_ready:
            mode = self.engine.ask(":WAV:POIN:MODE?").strip().upper()
            if mode not in {"NORM", "NORMAL"}:
                self._write(":WAV:POIN:MODE NORM", settle=True)
            self._waveform_mode_ready = True
        self._wait_settled()
        ch = self._ch_cache.get(channel) or self.read_channel(channel)
        tb = self._tb or self.read_timebase()

        resp = self.engine.ask_binary(f":WAV:DATA? CHAN{channel}")
        payload, _ = parse_ieee_block(resp)
        codes = np.frombuffer(payload, dtype=np.uint8)
        if codes.size != 600:
            self._waveform_mode_ready = False
            raise RuntimeError(f"CH{channel} 屏幕数据应为 600 点，实际 {codes.size} 点；请重试")

        # 屏幕坐标向下增大：码125是网格中心，25码/格。
        v = (125.0 - codes.astype(np.float64)) * (ch.scale / 25.0) - ch.offset
        n = codes.size
        wf = Waveform(
            channel=f"CH{channel}",
            y=v,
            x_start=tb.position - 6.0 * tb.scale,
            x_incr=12.0 * tb.scale / n,
            raw=codes,
            source_encoding="BYTE",
            full_record=False,  # NORM is the 600-point screen trace, not native memory.
        )
        wf.decode_note = (
            f"DS1000E BYTE {n}点 scale={ch.scale:g}V/div offset={ch.offset:g}V"
        )
        return wf

    # ---------------- 屏幕截图 ----------------
    def capture_screen(self) -> bytes:
        """DS1000E 无 :DISPlay:DATA?，无法远程回传屏幕图像。

        设备仅支持 :HARDcopy（屏幕存 BMP 到 U 盘）。远程路径用
        render_screen_snapshot() 按设备真实数据重建屏幕画面。
        """
        raise NotImplementedError(
            "DS1000E 不支持远程回传屏幕图像（仅 :HARDcopy 存 U 盘）；"
            "已按设备真实数据重建屏幕画面"
        )

    def render_screen_snapshot(self) -> bytes:
        """用设备真实波形+设置重建一张设备风格屏幕 PNG。"""
        from ..screen_render import png_bytes, render_annotated_screen

        waveforms = self.fetch_all()
        channels = {
            f"CH{n}": self.read_channel(n) for n in range(1, self.channel_count + 1)
        }
        tb = self.read_timebase()
        img = render_annotated_screen(waveforms, channels, tb)
        return png_bytes(img)
