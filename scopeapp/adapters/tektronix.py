"""Tektronix 适配器（MSO44B / 4-5-6 系列 MSO 优先，兼容现代 DPO/MSO 家族）。

波形路径（依据 4/5/6 系列 MSO 编程手册 Appendix D）：
    :DATa:SOUrce CHn
    :WFMOutpre:ENCdg BINARY / :WFMOutpre:BYT_Nr 1|2
    :WFMOutpre?   -> 返回 "KEY value;KEY value;..." 键值对（无固定字段顺序）
    :CURVe?       -> 返回 #N<len> 原始二进制块（无内嵌头）
换算：y = (raw - YOFF) * YMULT + YZERO；x = (i - PT_OFF) * XINCR + XZERO
（YOFF 单位 digitizing levels，25 levels/div；XZERO 为触发子采样时间）
"""
from __future__ import annotations

import re
import math
import uuid
from contextlib import contextmanager
from typing import Optional

import numpy as np

from ..model import (
    ChannelSettings,
    Coupling,
    TimebaseSettings,
    TriggerSettings,
    Waveform,
    WaveformEncoding,
)
from ..scpi import parse_ieee_block
from ..waveform import DecodeOptions, decode_waveform
from .base import ScopeAdapter
from .registry import adapters_registry

_MAX_TRANSFER_POINTS = 100_000
_FULL_RECORD_CHUNK_POINTS = 500_000


def parse_wfmoutpre(text: str) -> dict[str, str]:
    """解析 :WFMOutpre? 的 "KEY value;KEY value;..." 键值对响应。

    兼容 HEADER ON（首 token 形如 :WFMOUTPRE:BYT_NR 1）与 HEADER OFF。
    """
    out: dict[str, str] = {}
    parts = re.split(r';(?=(?:[^"]*"[^"]*")*[^"]*$)', text.strip())
    # HEADER OFF 是位置字段；新系列比旧系列多 PT_ORDER 字段。
    if parts and re.fullmatch(r"\d+", parts[0].strip()):
        keys = ["BYT_NR", "BIT_NR", "ENCDG", "BN_FMT", "BYT_OR", "WFID",
                "NR_PT", "PT_FMT", "PT_ORDER", "XUNIT", "XINCR", "XZERO",
                "PT_OFF", "YUNIT", "YMULT", "YOFF", "YZERO"]
        if len(parts) == 16:
            keys.remove("PT_ORDER")
        if len(parts) < len(keys):
            raise ValueError("Tektronix 波形前导字段不完整")
        return {key: val.strip().strip('"') for key, val in zip(keys, parts)}
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if " " in part:
            key, _, val = part.partition(" ")
        elif ":" in part:
            key, _, val = part.partition(":")
        else:
            continue
        key = key.strip().upper().rstrip("?").split(":")[-1]
        out[key] = val.strip().strip('"')
    return out


def _num(d: dict[str, str], key: str) -> float:
    try:
        value = float(d[key])
        if not math.isfinite(value):
            raise ValueError
        return value
    except (KeyError, ValueError):
        raise ValueError(f"Tektronix 前导参数 {key} 缺失或无效，拒绝换算波形") from None


@adapters_registry.register
class TektronixAdapter(ScopeAdapter):
    """Tektronix MSO/DPO 系列适配器。"""

    name = "Tektronix (MSO44 / MSO44B / 4-5-6 系列)"
    idn_pattern = re.compile(r"tektronix", re.IGNORECASE)
    channel_count = 4
    supported_couplings = ("DC", "AC")
    supported_trigger_slopes = ("RISE", "FALL", "EITHER")
    supported_trigger_modes = ("AUTO", "NORMAL", "SINGLE")

    def __init__(self, engine):
        model = engine.identity.model.upper() if engine.identity else ""
        self._modern = bool(re.match(r"(?:MSO[456][468]|LPD6)", model))
        super().__init__(engine)
        self.error_query_supported = False  # Tek 使用事件队列，不支持 SYST:ERR?。
        self.engine.write(":HEADer OFF;:VERBose ON")
        self.preview_points = 10000
        self._record_length = 0
        self._transfer_stride = 1
        self._transfer_points = 0
        self._trigger_source = "CH1"
        # 现代 Tek 默认传输真实完整记录，预览仅供显式诊断调用。
        self.full_record = self._modern
        self.transfer_chunk_points = _FULL_RECORD_CHUNK_POINTS
        self.waveform_progress = None
        self.frame_settings_callback = None
        self._full_transaction = False
        self._full_record_points = 0
        self._progress_points = 0
        self._progress_total = 0
        self._restoring_protocol = False
        self._frame_running = False

    def _float(self, command: str) -> float:
        value = float(self.engine.ask(command))
        if not math.isfinite(value):
            raise ValueError(f"{command} 返回无效数值")
        return value

    def _enabled(self, number: int) -> bool:
        command = (f":DISplay:GLObal:CH{number}:STATE?" if self._modern
                   else f":SELect:CH{number}?")
        return self._float(command) != 0

    # ---------------- 状态 ----------------
    def run(self) -> None:
        self.engine.write(":ACQuire:STOPAfter RUNSTop;:ACQuire:STATE RUN")

    def single(self) -> None:
        self.engine.write(":ACQuire:STOPAfter SEQuence;:ACQuire:STATE RUN")

    def force_trigger(self) -> None:
        self.engine.write(":TRIGger FORCe")

    def autoset(self) -> None:
        self.engine.write(":AUTOSet EXECute")
        self._invalidate_preamble()

    def set_trigger_mode(self, mode: str) -> None:
        self.engine.write(f":TRIGger:A:MODe {mode}")

    def set_trigger_slope(self, slope: str) -> None:
        self.engine.write(f":TRIGger:A:EDGE:SLOpe {slope}")

    def set_channel_coupling(self, number: int, coupling: str) -> None:
        self.engine.write(f"CH{number}:COUPling {coupling}")
        self._invalidate_preamble()

    def stop(self) -> None:
        self.engine.write(":ACQuire:STATE STOP")

    def is_running(self) -> bool:
        return self._float(":ACQuire:STATE?") == 1

    # ---------------- 设置 ----------------
    def read_timebase(self) -> TimebaseSettings:
        scale = self._float(":HORizontal:SCAle?")
        sample_rate = self._float(":HORizontal:SAMPLERate?")
        self._record_length = int(self._float(":HORizontal:RECOrdlength?"))
        if scale <= 0 or sample_rate <= 0 or self._record_length <= 0:
            raise ValueError("Tektronix 时基、采样率或记录长度无效")
        divisions, position = 10, 0.0
        if self._modern:
            divisions = int(self._float(":HORizontal:DIVisions?"))
            if divisions <= 0:
                raise ValueError("Tektronix 水平格数无效")
            if self._float(":HORizontal:DELay:MODe?") != 0:
                position = self._float(":HORizontal:DELay:TIMe?")
            else:
                percent = self._float(":HORizontal:POSition?")
                position = (50 - percent) / 100 * divisions * scale
        return TimebaseSettings(scale=scale, position=position, sample_rate=sample_rate,
                                record_length=self._record_length, horizontal_divisions=divisions)

    def read_channel(self, number: int) -> ChannelSettings:
        pre = f"CH{number}:"
        coup = self.engine.ask(f"{pre}COUPling?").strip().upper()
        bandwidth = self.engine.ask(f"{pre}BANdwidth?").strip().upper()
        if bandwidth in {"FULL", "FUL"}:
            bandwidth = "FULL"
        else:
            try:
                hz = float(bandwidth)
                bandwidth = (f"{hz / 1e6:g} MHz" if hz >= 1e6 else f"{hz:g} Hz") if math.isfinite(hz) and hz > 0 else None
            except ValueError:
                bandwidth = None
        return ChannelSettings(
            number=number,
            enabled=self._enabled(number),
            scale=self._float(f"{pre}SCAle?"),
            offset=self._float(f"{pre}OFFSet?"),
            vertical_position=self._float(f"{pre}POSition?") if self._modern else 0,
            bandwidth=bandwidth,
            coupling=Coupling(coup) if coup in {c.value for c in Coupling} else Coupling.DC,
        )

    def read_trigger(self) -> TriggerSettings:
        source = self.engine.ask(":TRIGger:A:EDGE:SOUrce?").strip().upper()
        if not re.fullmatch(r"CH\d", source):
            raise ValueError(f"当前触发源 {source!r} 尚未适配；请选择模拟通道边沿触发")
        self._trigger_source = source
        return TriggerSettings(
            source=source,
            level=self._float(f":TRIGger:A:LEVel:{source}?"),
            slope=self.engine.ask(":TRIGger:A:EDGE:SLOpe?").strip().upper(),
            mode=self.engine.ask(":TRIGger:A:MODe?").strip().upper(),
        )

    def set_timebase(self, scale: float) -> None:
        self.engine.write(f":HORizontal:SCAle {scale:g}")
        self._invalidate_preamble()

    def set_channel_scale(self, number: int, scale: float) -> None:
        self.engine.write(f"CH{number}:SCAle {scale:g}")
        self._invalidate_preamble()

    def set_channel_offset(self, number: int, offset: float) -> None:
        self.engine.write(f"CH{number}:OFFSet {offset:g}")
        self._invalidate_preamble()

    def set_channel_enabled(self, number: int, enabled: bool) -> bool:
        command = (f":DISplay:GLObal:CH{number}:STATE" if self._modern
                   else f":SELect:CH{number}")
        self.engine.write(f"{command} {'ON' if enabled else 'OFF'}")
        actual = self._enabled(number)
        self._enabled_cache[number] = actual
        return actual == enabled

    def set_trigger_level(self, level: float) -> None:
        source = self.engine.ask(":TRIGger:A:EDGE:SOUrce?").strip().upper()
        if not re.fullmatch(r"CH\d", source):
            raise ValueError("当前触发源不支持模拟通道电平设置")
        self.engine.write(f":TRIGger:A:LEVel:{source} {level:g}")

    def set_trigger_source(self, source: str) -> None:
        self.engine.write(f":TRIGger:A:EDGE:SOUrce {source}")
        self._trigger_source = source

    # ---------------- 波形 ----------------
    def _invalidate_preamble(self) -> None:
        # 本版每帧重新读取前导；保留调用点，避免未来优化遗漏失效条件。
        pass

    def set_preview_points(self, points: int):
        """显式诊断预览；上位机正常采集/保存不调用此入口。"""
        self.preview_points = min(_MAX_TRANSFER_POINTS, max(100, int(points)))
        self.full_record = False
        self._invalidate_preamble()

    def set_full_record(self) -> None:
        self.full_record = True
        self._invalidate_preamble()

    def _record_points(self) -> int:
        value = self._float(":HORizontal:RECOrdlength?")
        if value <= 0 or not value.is_integer():
            raise ValueError("Tektronix 记录长度不是有效整数")
        self._record_length = int(value)
        return self._record_length

    @contextmanager
    def _record_transfer(self, channel_count: int):
        """只读采集状态；RUN 单条命令读回，用户 STOP 后才分块读取。"""
        if self._full_transaction:
            yield
            return
        running = self.is_running()
        stop_after = self.engine.ask(":ACQuire:STOPAfter?").strip().upper()
        if stop_after not in ("RUNSTOP", "RUNST", "SEQUENCE", "SEQ"):
            raise ValueError(f"Tektronix 返回未知采集停止方式 {stop_after!r}")
        if running and stop_after in ("SEQUENCE", "SEQ"):
            raise RuntimeError("Single 尚未完成，请等待触发后读取完整记录")
        try:
            self._frame_running = running
            acquisition_count = None if running else self._float(":ACQuire:NUMACq?")
            self._full_record_points = self._record_points()
            if self.frame_settings_callback:
                self.frame_settings_callback()
                if self._record_points() != self._full_record_points:
                    raise ValueError("Tektronix 记录长度在设置读回期间变化，拒绝读取")
            self._progress_points = 0
            self._progress_total = self._full_record_points * channel_count
            self._full_transaction = True
            yield
            if self._record_points() != self._full_record_points:
                raise ValueError("Tektronix 记录长度在传输期间变化，拒绝保存不完整记录")
            if not running and self.is_running():
                raise ValueError("Tektronix 在传输期间重新运行，拒绝拼接不同采集的数据")
            if not running and self._float(":ACQuire:NUMACq?") != acquisition_count:
                raise ValueError("Tektronix 记录在传输期间被新采集刷新，请手动 STOP 后重新读取")
        finally:
            self._full_transaction = False
            self._frame_running = False

    def _report_progress(self, channel: int, channel_points: int):
        if self.waveform_progress:
            self.waveform_progress({
                "channel": f"CH{channel}", "channel_points": channel_points,
                "record_points": self._full_record_points,
                "received_points": self._progress_points,
                "total_points": self._progress_total,
                "running": self._frame_running,
            })

    def _require_available_sources(self, channels: list[int]) -> None:
        """缺少已采集记录时不发送 CURVE?，避免无二进制响应造成超时。"""
        if not channels:
            return
        response = self.engine.ask(":DATa:SOUrce:AVAILable?").strip().upper()
        # 初始化使用 HEADER OFF；兼容带完整或缩写头的合法响应。
        response = re.sub(r"^:?(?:DATA|DAT):(?:SOURCE|SOU):(?:AVAILABLE|AVAIL)\s+", "", response)
        response = response.strip('"')
        if not response:
            raise ValueError("Tektronix 未返回有效的已采集波形源列表")
        tokens = {token.strip().strip('"') for token in response.split(",")}
        if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", token) for token in tokens):
            raise ValueError("Tektronix 已采集波形源列表格式无效")
        available = set() if tokens == {"NONE"} else tokens
        missing = [f"CH{number}" for number in channels if f"CH{number}" not in available]
        if missing:
            raise RuntimeError(
                f"{', '.join(missing)} 尚无已采集记录；请先 RUN 或 Single 完成采集后读取。"
                "启用通道不代表已经产生波形，未发送 CURVE?。"
            )

    def fetch_all(self) -> dict[str, Waveform]:
        # 任一通道超时即报告具体命令错误，不能吞掉错误再查询 SYST:ERR?。
        channels = [n for n in range(1, self.channel_count + 1)
                    if self._enabled_cache.get(n, True)]
        if not channels:
            return {}
        if self.full_record and self._modern:
            with self._record_transfer(len(channels)):
                channels = [n for n in range(1, self.channel_count + 1)
                            if self._enabled_cache.get(n, True)]
                self._progress_total = self._full_record_points * len(channels)
                self._require_available_sources(channels)
                fetch = self._fetch_live_waveform if self._frame_running else self._fetch_full_waveform
                return {f"CH{n}": fetch(n) for n in channels}
        return {f"CH{n}": self.fetch_waveform(n) for n in channels}

    def _prepare_source(self, channel: int) -> None:
        """复合命令一次往返完成波形源准备（含起始/停止点）。"""
        # NR_PT 是本次传输长度，不能反过来当作下一帧的请求上限。
        record_length = int(self._float(":HORizontal:RECOrdlength?"))
        if record_length <= 0:
            raise ValueError("Tektronix 记录长度无效")
        self._record_length = record_length
        cap = min(_MAX_TRANSFER_POINTS, max(100, int(self.preview_points)))
        stride = math.ceil(record_length / cap) if self._modern else 1
        stop = record_length if self._modern else min(record_length, cap)
        self._transfer_stride = stride
        self._transfer_points = math.ceil(stop / stride)
        resample = f":DATa:RESample {stride};" if self._modern else ""
        self.engine.write(
            f":DATa:SOUrce CH{channel};:DATa:STARt 1;:DATa:STOP {stop};{resample}"
            f":WFMOutpre:ENCdg BINARY;:WFMOutpre:BYT_Nr 2;"
            f":WFMOutpre:BN_Fmt RI;:WFMOutpre:BYT_Or MSB"
        )

    def _read_preamble(self) -> dict[str, str]:
        self.engine.write(":HEADer ON")
        try:
            return parse_wfmoutpre(self.engine.ask(":WFMOutpre?"))
        finally:
            self._restoring_protocol = True
            try:
                self.engine.write(":HEADer OFF")
            finally:
                self._restoring_protocol = False

    def _get_preamble(self, channel: int) -> dict[str, str]:
        # 前面板修改时基/量程也改变换算参数，每帧读取，防止旧参数解码新记录。
        return self._read_preamble()

    def _decode(self, payload: bytes, pre: dict[str, str], channel: int) -> Waveform:
        byte_count = _num(pre, "BYT_NR")
        if byte_count not in (1, 2) or pre.get("BN_FMT", "").upper() != "RI" or pre.get("ENCDG", "").upper() not in ("BIN", "BINARY"):
            raise ValueError("Tektronix 返回的波形编码不是已适配的 RI 二进制")
        byt_nr = int(byte_count)
        if pre.get("PT_FMT", "").upper() != "Y":
            raise ValueError("当前采集为包络等未适配格式，请切换到 Sample / HiRes / Average")
        if pre.get("BYT_OR", "").upper() not in ("MSB", "LSB"):
            raise ValueError("Tektronix 波形字节序缺失")
        points = len(payload) // byt_nr
        if len(payload) % byt_nr or points != _num(pre, "NR_PT") or points > self._transfer_points:
            raise ValueError("Tektronix 波形长度与前导/传输上限不符，拒绝显示")
        encoding = WaveformEncoding.INT8 if byt_nr <= 1 else WaveformEncoding.INT16
        byte_order = "<" if pre.get("BYT_OR", "MSB").upper() == "LSB" else ">"
        opts = DecodeOptions(
            encoding=encoding,
            byte_order=byte_order,
            x_incr=_num(pre, "XINCR"),
            x_origin=_num(pre, "XZERO"),
            x_reference=_num(pre, "PT_OFF"),
            y_incr=_num(pre, "YMULT"),
            y_origin=_num(pre, "YZERO"),
            y_reference=_num(pre, "YOFF"),
        )
        if opts.x_incr <= 0 or opts.y_incr <= 0:
            raise ValueError("Tektronix 波形采样间隔/电压增量无效")
        wf = decode_waveform(payload, opts, channel=f"CH{channel}")
        wf.decode_note = (
            f"WFMOutpre: ENCDG={pre.get('ENCDG')} BYT_NR={byt_nr} "
            f"NR_PT={_num(pre, 'NR_PT'):.0f} XINCR={opts.x_incr:g}s; "
            f"设备抽取步长={self._transfer_stride}（非完整深存储记录）"
        )
        return wf

    def _fetch_full_waveform(self, channel: int) -> Waveform:
        depth = self._full_record_points
        chunk_points = int(self.transfer_chunk_points)
        if chunk_points <= 0:
            raise ValueError("Tektronix 完整记录块大小必须大于零")
        self._transfer_stride = 1
        self.engine.write(
            f":DATa:SOUrce CH{channel};:DATa:RESample 1;"
            ":WFMOutpre:ENCdg BINARY;:WFMOutpre:BYT_Nr 2;"
            ":WFMOutpre:BN_Fmt RI;:WFMOutpre:BYT_Or MSB"
        )
        result = None
        first_pre = None
        for offset in range(0, depth, chunk_points):
            if self.is_running():
                raise ValueError("Tektronix 在分块传输期间重新运行，拒绝拼接不同采集的数据")
            count = min(chunk_points, depth - offset)
            self._transfer_points = count
            self.engine.write(f":DATa:STARt {offset + 1};:DATa:STOP {offset + count}")
            pre = self._get_preamble(channel)
            if _num(pre, "NR_PT") != count:
                raise ValueError(
                    f"CH{channel} 完整记录第 {offset + 1} 点起应返回 {count} 点，"
                    f"前导实际 {_num(pre, 'NR_PT'):g} 点，拒绝抽取或补点"
                )
            if pre.get("XUNIT", "").lower() != "s" or pre.get("YUNIT", "").upper() != "V":
                raise ValueError(f"CH{channel} 前导不是时间/电压单位，拒绝套用默认单位")
            response = self.engine.ask_binary(":CURVe?")
            if not response.startswith(b"#"):
                raise ValueError("CURVE? 未返回 IEEE 二进制块")
            payload, _ = parse_ieee_block(response)
            block = self._decode(payload, pre, channel)
            if block.points != count:
                raise ValueError(f"CH{channel} 数据块长度不完整，拒绝补点")
            if result is None:
                first_pre = pre
                result = Waveform(
                    channel=f"CH{channel}", y=np.empty(depth, dtype=np.float64),
                    raw=np.empty(depth, dtype=block.raw.dtype),
                    x_start=block.x_start, x_incr=block.x_incr,
                    source_encoding=block.source_encoding,
                )
            else:
                for key in ("BYT_NR", "BIT_NR", "ENCDG", "BN_FMT", "BYT_OR", "PT_FMT", "PT_ORDER", "XUNIT", "YUNIT"):
                    if pre.get(key) != first_pre.get(key):
                        raise ValueError(f"CH{channel} 块间前导 {key} 改变，拒绝拼接")
                for key in ("XINCR", "XZERO", "YMULT", "YOFF", "YZERO"):
                    if not math.isclose(_num(pre, key), _num(first_pre, key), rel_tol=1e-12, abs_tol=1e-18):
                        raise ValueError(f"CH{channel} 块间换算参数 {key} 改变，拒绝拼接")
                if abs(_num(pre, "PT_OFF") - (_num(first_pre, "PT_OFF") - offset)) > .01:
                    raise ValueError(f"CH{channel} 数据块触发参考点不连续，拒绝插值或拼接")
                expected_start = result.x_start + offset * result.x_incr
                # 容差小于百分之一个采样间隔，既容纳浮点舍入又拒绝错位/重叠。
                tolerance = max(result.x_incr * .01, abs(expected_start) * 1e-12)
                if abs(block.x_start - expected_start) > tolerance:
                    raise ValueError(f"CH{channel} 数据块时间轴不连续，拒绝插值或拼接")
            result.y[offset:offset + count] = block.y
            result.raw[offset:offset + count] = block.raw
            self._progress_points += count
            self._report_progress(channel, offset + count)
        if result is None or result.points != depth:
            raise ValueError(f"CH{channel} 未读到真实完整记录")
        result.device_record_length = depth
        result.sample_stride = 1
        result.full_record = True
        result.record_stable = True
        result.decode_note = (
            f"完整设备记录 {depth} 点；DATA:RESAMPLE 1；"
            f"{math.ceil(depth / chunk_points)} 个连续数据块；"
            f"XINCR={result.x_incr:g}s；无抽取、插值或补点"
        )
        return result

    def _fetch_live_waveform(self, channel: int) -> Waveform:
        """运行中一次读完整 WAVFrm，避免把多次触发的数据块拼在一起。"""
        depth = self._full_record_points
        self._transfer_stride, self._transfer_points = 1, depth
        self.engine.write(
            f":DATa:SOUrce CH{channel};:DATa:RESample 1;:DATa:STARt 1;:DATa:STOP {depth};"
            ":WFMOutpre:ENCdg BINARY;:WFMOutpre:BYT_Nr 2;:WFMOutpre:BN_Fmt RI;:WFMOutpre:BYT_Or MSB")
        self._report_progress(channel, 0)
        self.engine.write(":HEADer ON")
        try:
            # 前导与数据通过同一条指令返回。大记录只延长本次传输超时。
            timeout_ms = max(5000, min(120000, 5000 + math.ceil(depth * 2 / 2_000_000) * 1000))
            response = self.engine.ask_binary_with_timeout(":WAVFrm?", timeout_ms)
        finally:
            self._restoring_protocol = True
            try:
                self.engine.write(":HEADer OFF")
            finally:
                self._restoring_protocol = False
        block_index = response.find(b"#")
        if block_index < 0:
            raise ValueError("WAVFrm? 未返回完整二进制数据块")
        pre = parse_wfmoutpre(response[:block_index].decode("utf-8", errors="strict"))
        if _num(pre, "NR_PT") != depth or pre.get("XUNIT", "").lower() != "s" or pre.get("YUNIT", "").upper() != "V":
            raise ValueError("运行中完整波形点数或单位无效，拒绝抽点或补点")
        payload, _ = parse_ieee_block(response[block_index:])
        result = self._decode(payload, pre, channel)
        if result.points != depth:
            raise ValueError("运行中完整波形缺少真实采样点")
        result.device_record_length, result.sample_stride, result.full_record = depth, 1, True
        result.record_stable = False
        result.decode_note = f"运行中 WAVFrm 单次读回完整 {depth} 点；无抽点；各通道依次读取，未确认同次触发"
        self._progress_points += depth
        self._report_progress(channel, depth)
        return result

    def fetch_waveform(self, channel: int) -> Waveform:
        if self.full_record and self._modern:
            with self._record_transfer(1):
                self._require_available_sources([channel])
                return self._fetch_live_waveform(channel) if self._frame_running else self._fetch_full_waveform(channel)
        self._prepare_source(channel)
        pre = self._get_preamble(channel)
        resp = self.engine.ask_binary(":CURVe?")
        if not resp.startswith(b"#"):
            raise ValueError("CURVE? 未返回 IEEE 二进制块")
        payload, _ = parse_ieee_block(resp)
        wf = self._decode(payload, pre, channel)
        wf.device_record_length = self._record_length
        wf.sample_stride = self._transfer_stride
        wf.full_record = False
        return wf

    def fetch_wavfrm(self, channel: int) -> Waveform:
        """通过 :WAVFrm?（WFMOutpre? + CURVe? 拼接）读取波形，备用路径。"""
        if self.full_record and self._modern:
            # 完整记录统一按用户采集状态选择整包或分块读取。
            return self.fetch_waveform(channel)
        self._prepare_source(channel)
        self._invalidate_preamble()
        self.engine.write(":HEADer ON")
        try:
            resp = self.engine.ask_binary(":WAVFrm?")
        finally:
            self.engine.write(":HEADer OFF")
        # 响应 = 前导文本（键值对）+ LF + #N<len> 二进制块
        block_idx = resp.find(b"#")
        if block_idx < 0:
            raise ValueError("WAVFrm? 响应中未找到二进制块")
        pre_text = resp[:block_idx].decode("utf-8", errors="replace")
        payload, _ = parse_ieee_block(resp[block_idx:])
        pre = parse_wfmoutpre(pre_text)
        wf = self._decode(payload, pre, channel)
        wf.decode_note = "via :WAVFrm?"
        return wf

    # ---------------- 屏幕截图（真实设备屏幕） ----------------
    def capture_screen(self) -> bytes:
        """抓取 MSO44 / MSO44B 真实屏幕，返回 PNG 字节。

        流程（依据 4/5/6 系列手册）：
            1. :SAVe:IMAGe "C:/scopehost_capture_<唯一标识>.png" —— 保存临时图像
            2. 等待 *OPC? 后 :FILESystem:READFile ...  —— 读回字节流
            3. :FILESystem:DELEte ...                    —— 清理临时文件
        """
        # 独立文件名防止覆盖已有文件，也避免预先删除不存在文件产生仪器事件。
        path = f"C:/scopehost_capture_{uuid.uuid4().hex}.png"
        self.engine.write(f':SAVe:IMAGe "{path}"')
        try:
            if self.engine.ask("*OPC?").strip() != "1":
                raise RuntimeError("示波器截图尚未保存完成")
            resp = self.engine.ask_binary(f':FILESystem:READFile "{path}"')
            payload, _ = parse_ieee_block(resp)
            if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("仪器未返回有效的 PNG 屏幕图像")
            return payload
        finally:
            try:
                self.engine.write(f':FILESystem:DELEte "{path}"')
            except Exception:  # noqa: BLE001
                pass
