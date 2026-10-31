"""DLM3000: IM DLM3054-17EN, 7th edition (July 2025).

Live: WAVeform:ALL:SEND? is a single acquisition shared by all traces.
Stopped/Single: WAVeform:SEND? reads WORD records in bounded transfers.
Voltage = code * WAVeform:RANGe / 3200 + WAVeform:OFFSet.
Time = (sample_index - WAVeform:TRIGger) / WAVeform:SRATe.
"""
from __future__ import annotations

import math
import re
import struct

import numpy as np

from ..model import ChannelSettings, Coupling, TimebaseSettings, TriggerSettings, Waveform
from ..scpi import parse_ieee_block
from .base import ScopeAdapter
from .registry import adapters_registry


@adapters_registry.register
class YokogawaDlm3000Adapter(ScopeAdapter):
    name = "Yokogawa 横河 (DLM3000 系列)"
    idn_pattern = re.compile(r"yokogawa[\s,]+DLM30[235][24](?=[\s,(]|$)", re.I)
    supported_couplings = ("DC", "AC", "DC50")
    supported_trigger_slopes = ("RISE", "FALL", "BOTH")
    supported_trigger_modes = ("AUTO", "NORMAL", "SINGLE", "ALEVEL")
    default_tcp_port = 10002
    _TRANSFER_POINTS = 100_000
    _MAX_RECORD_POINTS = 2_000_000

    def __init__(self, engine):
        model = engine.identity.model.upper() if engine.identity else ""
        if not re.fullmatch(r"DLM30[235][24]", model):
            raise ValueError("横河适配器仅支持 DLM3022/3024/3032/3034/3052/3054")
        self.channel_count = int(model[-1])
        super().__init__(engine)
        # Yokogawa uses STATus:ERRor?, not the engine's SYSTem:ERRor?.
        self.error_query_supported = False
        self.engine.write(":COMMunicate:HEADer OFF")
        self.engine.write(":COMMunicate:REMote ON")
        for number in self._enabled_cache:
            self._enabled_cache[number] = self._integer(f":CHANnel{number}:DISPlay?") == 1
        if self.channel_count == 4 and self._integer(":LOGic:MODE?"):
            self._enabled_cache[4] = False

    def _ask(self, command: str) -> str:
        value = self.engine.ask(command).strip()
        # Accept an instrument that still includes a response header.
        if value.startswith(":"):
            value = value.split(None, 1)[-1].strip()
        if not value:
            raise ValueError(f"DLM3000 未返回 {command} 的值")
        return value.upper()

    def _number(self, command: str, *, positive: bool = False) -> float:
        value = float(self._ask(command))
        if not math.isfinite(value) or (positive and value <= 0):
            raise ValueError(f"DLM3000 参数无效：{command} = {value}")
        return value

    def _integer(self, command: str, *, minimum: int = 0) -> int:
        value = self._number(command)
        if not value.is_integer() or value < minimum:
            raise ValueError(f"DLM3000 整数参数无效：{command} = {value}")
        return int(value)

    def _channel(self, number: int) -> str:
        if number not in range(1, self.channel_count + 1):
            raise ValueError(f"该 DLM3000 没有 CH{number}")
        return f":CHANnel{number}"

    def _voltage_channel(self, number: int) -> str:
        prefix = self._channel(number)
        if self._ask(f"{prefix}:PROBe:MODE?").startswith("C"):
            raise NotImplementedError(f"CH{number} 当前使用电流探头，上位机暂仅支持电压通道，不能将电流标为 V")
        return prefix

    def _edge_trigger(self) -> None:
        self.engine.write(":TRIGger:COMBination OFF")
        self.engine.write(":TRIGger:ATRigger:TYPE SIMPle")

    def run(self) -> None:
        mode = self._ask(":TRIGger:MODE?")
        if mode.startswith("NSIN"):
            self.engine.write(":TRIGger:MODE AUTO")
        self.engine.write(":STARt")

    def stop(self) -> None:
        self.engine.write(":STOP")

    def is_running(self) -> bool:
        return bool(self._integer(":STATus:CONDition?") & 1)

    def single(self) -> None:
        # 0 arms immediately; a positive timeout would block the acquisition worker.
        if self._integer(":SSTart? 0") != 0:
            raise RuntimeError("DLM3000 未成功启动 Single")

    def force_trigger(self) -> None:
        self.engine.write(":TRIGger:FORCe")

    def autoset(self) -> None:
        self.engine.write(":ASETup:EXECute")

    def set_trigger_mode(self, mode: str) -> None:
        if mode == "SINGLE":
            self.single()
        elif mode in {"AUTO", "NORMAL", "ALEVEL"}:
            self.engine.write(f":TRIGger:MODE {mode}")
        else:
            raise ValueError(f"不支持的触发模式：{mode}")

    def set_trigger_slope(self, slope: str) -> None:
        if slope not in {"RISE", "FALL", "BOTH"}:
            raise ValueError(f"不支持的触发边沿：{slope}")
        self._edge_trigger()
        self.engine.write(f":TRIGger:ATRigger:SIMPle:SLOPe {slope}")

    def set_trigger_source(self, source: str) -> None:
        match = re.fullmatch(r"CH([1-4])", source.upper())
        if not match:
            raise ValueError("DLM3000 上位机边沿触发源须为模拟通道")
        self._channel(int(match[1]))
        self._edge_trigger()
        self.engine.write(f":TRIGger:ATRigger:SIMPle:SOURce {match[1]}")

    def set_trigger_level(self, level: float) -> None:
        self._edge_trigger()
        self.engine.write(f":TRIGger:ATRigger:SIMPle:LEVel {level:g}")

    def set_timebase(self, scale: float) -> None:
        self.engine.write(f":TIMebase:TDIV {scale:g}")

    def set_record_length(self, points: int) -> None:
        if points not in (1250, 12500, 125000, 1250000):
            raise ValueError("请选择 DLM3000 支持的记录长度")
        self.engine.write(f":ACQuire:RLENgth {points}")

    def set_channel_scale(self, number: int, scale: float) -> None:
        self.engine.write(f"{self._channel(number)}:VDIV {scale:g}")

    def set_channel_offset(self, number: int, offset: float) -> None:
        self.engine.write(f"{self._channel(number)}:OFFSet {offset:g}")

    def set_channel_coupling(self, number: int, coupling: str) -> None:
        if coupling not in self.supported_couplings:
            raise ValueError("DLM3000 支持 AC、DC、DC50 耦合，不支持 GND")
        self.engine.write(f"{self._channel(number)}:COUPling {coupling}")

    def set_channel_enabled(self, number: int, enabled: bool) -> bool:
        prefix = self._channel(number)
        if number == 4 and enabled and self._integer(":LOGic:MODE?"):
            raise RuntimeError("CH4 已切换为逻辑输入，请在设备面板切回模拟 CH4")
        self.engine.write(f"{prefix}:DISPlay {'ON' if enabled else 'OFF'}")
        actual = self._integer(f"{prefix}:DISPlay?") == 1
        self._enabled_cache[number] = actual
        return actual == enabled

    def read_channel(self, number: int) -> ChannelSettings:
        prefix = self._voltage_channel(number)
        coupling = Coupling(self._ask(f"{prefix}:COUPling?"))
        bandwidth = self._ask(f"{prefix}:BWIDth?")
        if bandwidth != "FULL":
            hz = float(bandwidth)
            if not math.isfinite(hz) or hz <= 0:
                raise ValueError(f"CH{number} 带宽读回无效")
            bandwidth = f"{hz / 1e6:g} MHz" if hz >= 1e6 else f"{hz / 1e3:g} kHz" if hz >= 1e3 else f"{hz:g} Hz"
        enabled = self._integer(f"{prefix}:DISPlay?") == 1
        if number == 4 and self._integer(":LOGic:MODE?"):
            enabled = False
        return ChannelSettings(
            number=number, enabled=enabled,
            scale=self._number(f"{prefix}:VDIV?", positive=True),
            offset=self._number(f"{prefix}:OFFSet?"), coupling=coupling,
            bandwidth=bandwidth,
            vertical_position=self._number(f"{prefix}:POSition?"),
        )

    def read_timebase(self) -> TimebaseSettings:
        scale = self._number(":TIMebase:TDIV?", positive=True)
        position_pct = self._number(":TRIGger:POSition?")
        return TimebaseSettings(
            scale=scale, position=(50 - position_pct) / 100 * scale * 10,
            sample_rate=self._number(":TIMebase:SRATe?", positive=True),
            record_length=self._integer(":ACQuire:RLENgth?", minimum=2),
        )

    def read_trigger(self) -> TriggerSettings:
        mode = self._ask(":TRIGger:MODE?")
        modes = {"AUTO": "AUTO", "NORM": "NORMAL", "ALEV": "ALEVEL", "NSIN": "SINGLE"}
        if mode[:4] not in modes:
            raise ValueError(f"DLM3000 触发模式读回无效：{mode}")
        mode = modes[mode[:4]]
        kind = self._ask(":TRIGger:ATRigger:TYPE?")
        if not kind.startswith("SIMP") or self._ask(":TRIGger:COMBination?") != "OFF":
            raise NotImplementedError("DLM3000 当前为增强触发，上位机暂支持边沿触发；请在设备面板选择 Edge")
        source = self._ask(":TRIGger:ATRigger:SIMPle:SOURce?")
        if source.isdigit():
            source = f"CH{source}"
        if not re.fullmatch(r"CH[1-4]", source):
            raise NotImplementedError(f"DLM3000 当前触发源为 {source}，上位机暂支持模拟通道边沿触发；请选择 CH1–CH{self.channel_count}")
        self._channel(int(source[-1]))
        slope = self._ask(":TRIGger:ATRigger:SIMPle:SLOPe?")
        if slope not in self.supported_trigger_slopes:
            raise ValueError(f"DLM3000 触发边沿读回无效：{slope}")
        return TriggerSettings(
            source=source, mode=mode,
            level=self._number(":TRIGger:ATRigger:SIMPle:LEVel?"),
            slope=slope,
        )

    def _metadata(self, channel: int) -> dict:
        self._voltage_channel(channel)
        self.engine.write(f":WAVeform:TRACe {channel};:WAVeform:RECord 0")
        rate = self._number(":WAVeform:SRATe?", positive=True)
        length = self._integer(":WAVeform:LENGth?", minimum=2)
        trigger = self._number(":WAVeform:TRIGger?")
        scale = self._number(":WAVeform:RANGe?", positive=True) / 3200
        offset = self._number(":WAVeform:OFFSet?")
        if self._integer(":SYSTem:OCANcel?"):
            # Return physical input voltage even when the instrument cancels offset
            # in its measurement/math results (manual 5-249, 5-327).
            offset += self._number(f":CHANnel{channel}:OFFSet?")
        signed = self._integer(":WAVeform:SIGN?")
        if signed not in (0, 1):
            raise ValueError("DLM3000 波形符号标记无效")
        return dict(rate=rate, length=length, trigger=trigger, scale=scale,
                    offset=offset, dtype="<i2" if signed else "<u2")

    @staticmethod
    def _decode(channel: int, raw: np.ndarray, meta: dict, start: int = 0, note: str = "") -> Waveform:
        return Waveform(
            channel=f"CH{channel}", y=raw.astype(np.float64) * meta["scale"] + meta["offset"],
            x_start=(start - meta["trigger"]) / meta["rate"], x_incr=1 / meta["rate"],
            raw=raw.copy(), source_encoding="INT16" if meta["dtype"] == "<i2" else "UINT16",
            decode_note=f"DLM3000 WORD; y=code*RANGE/3200+OFFSET; {note}",
        )

    def fetch_waveform(self, channel: int) -> Waveform:
        if self.is_running():
            return self.fetch_all()[f"CH{channel}"]
        meta = self._metadata(channel)
        length = meta["length"]
        if length > self._MAX_RECORD_POINTS:
            raise RuntimeError("DLM3000 当前记录超过 200 万点/通道，请降低设备记录长度后读取；未截断或替换波形")
        self.engine.write(":WAVeform:FORMat WORD;:WAVeform:BYTeorder LSBFirst")
        raw = np.empty(length, dtype=meta["dtype"])
        for start in range(0, length, self._TRANSFER_POINTS):
            end = min(length, start + self._TRANSFER_POINTS)
            self.engine.write(f":WAVeform:STARt {start};:WAVeform:END {end - 1}")
            payload, _ = parse_ieee_block(self.engine.ask_binary(":WAVeform:SEND?"))
            if len(payload) != (end - start) * 2:
                raise ValueError("DLM3000 波形数据长度与请求点数不符")
            raw[start:end] = np.frombuffer(payload, dtype=meta["dtype"])
        return self._decode(channel, raw, meta, note=f"完整记录 {length} 点")

    def fetch_all(self) -> dict[str, Waveform]:
        channels = [n for n, enabled in self._enabled_cache.items() if enabled]
        if not channels:
            return {}
        if not self.is_running():
            return {f"CH{n}": self.fetch_waveform(n) for n in channels}
        metadata = {n: self._metadata(n) for n in channels}
        # The documented live transfer is limited to 12.5 kPoints. Never guess a
        # decimation interval for a shorter reply, which could invent frequencies.
        if any(meta["length"] > 12_500 for meta in metadata.values()):
            raise RuntimeError("DLM3000 实时传输最多 12.5k 点/记录；请在设备设置较短记录，或用 Stop/Single 读取长记录")
        mode = self._ask(":TRIGger:MODE?")
        if mode.startswith("NSIN"):
            raise RuntimeError("DLM3000 Single 正在等待触发，请等待完成或强制触发")
        self.engine.write(":WAVeform:ALL:TRACe ALL")
        payload, _ = parse_ieee_block(self.engine.ask_binary(":WAVeform:ALL:SEND? 0"))
        if len(payload) < 10:
            raise ValueError("DLM3000 实时数据块缺少公共头")
        trace_count, acquisition = struct.unpack_from("<HQ", payload)
        if not 1 <= trace_count <= (len(payload) - 10) // 20:
            raise ValueError("DLM3000 实时数据块的通道数无效")
        cursor, waveforms, seen = 10, {}, set()
        for _ in range(trace_count):
            if cursor + 16 > len(payload):
                raise ValueError("DLM3000 实时数据块的通道头截断")
            trace, points = struct.unpack_from("<I8xI", payload, cursor)
            cursor += 16
            if trace == 0 or trace in seen:
                raise ValueError("DLM3000 实时数据块的通道编号无效或重复")
            seen.add(trace)
            end = cursor + points * 2
            if points < 2 or points > 12_500 or end > len(payload):
                raise ValueError("DLM3000 实时数据块的采样点数无效或截断")
            if trace in metadata:
                meta = metadata[trace]
                if points != meta["length"]:
                    raise ValueError("DLM3000 实时点数与记录长度不符，拒绝推测时间轴")
                raw = np.frombuffer(payload[cursor:end], dtype=meta["dtype"])
                waveforms[f"CH{trace}"] = self._decode(trace, raw, meta, note=f"同步采集 #{acquisition}")
            cursor = end
        if cursor != len(payload) or set(channels) - seen:
            raise ValueError("DLM3000 实时数据块有额外数据或缺少启用通道")
        return waveforms

    def capture_screen(self) -> bytes:
        previous = self._ask(":IMAGe:FORMat?")
        try:
            self.engine.write(":IMAGe:FORMat PNG")
            payload, _ = parse_ieee_block(self.engine.ask_binary(":IMAGe:SEND?"))
            if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("DLM3000 未返回有效的 PNG 屏幕图像")
            return payload
        finally:
            self.engine.write(f":IMAGe:FORMat {previous}")
