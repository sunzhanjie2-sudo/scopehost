"""波形解码：把厂商原始字节流变成 (时间轴, 电压) 的 Waveform。

支持的格式：
  - ASCII 波形（逗号/空白分隔的浮点文本）
  - 二进制整型/浮点：INT8 / INT16 / INT32 / IEEE754（32/64 位），可指定字节序
  - TDS 风格内嵌头（CURVE? 的 25 字节头：Y offset 字节 + 6 个 float32 大端缩放参数）
  - Rigol 风格内嵌头（:WAV:DATA? 的 headless 标志 + Y origin/ref/int16 + Y incr/float32 LE）
  - Tektronix 4/5/6 系列 WAVFrm?（布局见 WAVFRM_LAYOUT，由真机手册核对）
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .model import Waveform, WaveformEncoding


def sample_bounds(wf: Waveform, start: float, end: float, *, pad: int = 0) -> tuple[int, int]:
    """返回时间闭区间内的真实采样索引 [first, stop)，无需构造整条时间轴。

    pad 仅用于绘图边界：包含左右相邻点，使裁剪后的折线连续。
    通过实际采样时间校正浮点除法在整数边界处的舍入误差。
    """
    n, dt = wf.points, wf.x_incr
    if not n or dt <= 0 or not math.isfinite(dt) or not math.isfinite(wf.x_start):
        return 0, 0
    if not math.isfinite(start) or not math.isfinite(end) or start > end:
        return 0, 0
    first = min(n, max(0, math.ceil((start - wf.x_start) / dt)))
    stop = min(n, max(0, math.floor((end - wf.x_start) / dt) + 1))
    while first > 0 and wf.x_start + (first - 1) * dt >= start:
        first -= 1
    while first < n and wf.x_start + first * dt < start:
        first += 1
    while stop > 0 and wf.x_start + (stop - 1) * dt > end:
        stop -= 1
    while stop < n and wf.x_start + stop * dt <= end:
        stop += 1
    padding = max(0, int(pad))
    return max(0, first - padding), min(n, stop + padding)


def display_sample_indices(wf: Waveform, start: float, end: float, pixel_width: float) -> np.ndarray:
    """按当前可见原始范围生成绘图索引；每个桶保留按时间排序的极值。

    少量点逐点绘制。密集记录最多保留每像素两个实际采样及边界点；
    降采样只影响绘图，不改变 Waveform，ZOOM 重新访问原始局部数据。
    """
    first, stop = sample_bounds(wf, start, end, pad=1)
    count = stop - first
    if count <= 0:
        return np.empty(0, dtype=np.intp)
    buckets = max(1, int(pixel_width))
    if count <= 2 * buckets:
        return np.arange(first, stop, dtype=np.intp)
    block_size = (count + buckets - 1) // buckets
    full = count // block_size * block_size
    blocks = wf.y[first:first + full].reshape(-1, block_size)
    bases = first + np.arange(len(blocks), dtype=np.intp) * block_size
    extrema = np.column_stack((bases + np.argmin(blocks, axis=1),
                               bases + np.argmax(blocks, axis=1)))
    middle = np.sort(extrema, axis=1).ravel()
    if full < count:
        tail = wf.y[first + full:stop]
        tail_extrema = first + full + np.sort([np.argmin(tail), np.argmax(tail)])
        middle = np.concatenate((middle, tail_extrema))
    indices = np.concatenate(([first], middle, [stop - 1]))
    return indices[np.r_[True, indices[1:] != indices[:-1]]]


def sample_value(wf: Waveform, time: float) -> Optional[float]:
    """用相邻真实采样计算游标读数，保持线性读数且不创建整条时间轴。"""
    if not wf.points or wf.x_incr <= 0 or not math.isfinite(time):
        return None
    last = wf.x_start + (wf.points - 1) * wf.x_incr
    if not wf.x_start <= time <= last:
        return None
    index = min(wf.points - 1, max(0, int((time - wf.x_start) / wf.x_incr)))
    if index == wf.points - 1:
        return float(wf.y[index])
    left = wf.x_start + index * wf.x_incr
    right = wf.x_start + (index + 1) * wf.x_incr
    fraction = (time - left) / (right - left)
    return float(wf.y[index] + fraction * (wf.y[index + 1] - wf.y[index]))


class DecodeError(Exception):
    pass


@dataclass
class DecodeOptions:
    """解码参数（多数由适配器从设备查询后填入）。"""

    encoding: WaveformEncoding = WaveformEncoding.INT16
    byte_order: str = ">"          # '>' 大端, '<' 小端
    points: int = 0
    x_incr: float = 1e-9           # s/采样点
    x_origin: float = 0.0          # 首个点时刻（s）
    x_reference: float = 0
    y_incr: float = 1.0            # V/码值
    y_origin: float = 0.0          # V
    y_reference: float = 0         # 码值；部分设备允许小数
    tds_header: bool = False       # payload 前 25 字节为 TDS 缩放头
    rigol_header: bool = False     # payload 前 10 字节为 Rigol 缩放头
    wavfrm: bool = False           # payload 为 Tektronix WAVFrm? 帧
    channel: str = "CH1"


# ---------------------------------------------------------------- 原始解码

def decode_raw(payload: bytes, encoding: WaveformEncoding, byte_order: str = ">") -> np.ndarray:
    """把二进制 payload 解码为 numpy 数组（int 或 float）。"""
    if encoding == WaveformEncoding.ASCII:
        text = payload.decode("utf-8", errors="replace")
        parts = text.replace(",", " ").split()
        return np.array([float(p) for p in parts], dtype=np.float64)

    dtype_map = {
        WaveformEncoding.INT8: "i1",
        WaveformEncoding.INT16: "i2",
        WaveformEncoding.INT32: "i4",
        WaveformEncoding.IEEE754: "f4",
    }
    code = dtype_map[encoding]
    if not payload:
        raise DecodeError("波形 payload 为空")
    dtype = np.dtype(f"{byte_order}{code}")
    n = len(payload) // dtype.itemsize
    if n == 0:
        raise DecodeError("payload 长度不足一个采样点")
    arr = np.frombuffer(payload, dtype=dtype, count=n)
    if encoding == WaveformEncoding.IEEE754:
        # 若字节数为 8 的倍数而非 4 的倍数，按 64 位浮点尝试
        if len(payload) % 8 == 0 and len(payload) % 4 != 0:
            arr = np.frombuffer(payload, dtype=f"{byte_order}f8")
    return arr


# ---------------------------------------------------------------- 头解析

def parse_tds_header(payload: bytes) -> "tuple[dict, int]":
    """解析 TDS 风格 25 字节内嵌头，返回 (缩放字段 dict, 数据起始偏移)。

    布局（TDS/DPO/MSO 系列 CURVE? 二进制响应）：
      byte 0        : Y offset（头字节数，典型 24）
      bytes 1-24    : 6 个 float32 大端：X increment, X origin, X reference,
                      Y increment, Y origin, Y reference
    换算：x = (i - xref) * xinc + xorigin；y = (raw - yref) * yinc + yorigin
    """
    if len(payload) < 25:
        raise DecodeError(f"TDS 头不足 25 字节（实际 {len(payload)}）")
    y_offset = payload[0]
    if y_offset + 1 > len(payload):
        raise DecodeError(f"TDS 头 Y offset 越界: {y_offset}")
    x_incr, x_origin, x_ref, y_incr, y_origin, y_ref = struct.unpack(">6f", payload[1:25])
    return {
        "x_incr": float(x_incr),
        "x_origin": float(x_origin),
        "x_reference": int(x_ref),
        "y_incr": float(y_incr),
        "y_origin": float(y_origin),
        "y_reference": int(y_ref),
    }, 1 + y_offset


def parse_rigol_header(payload: bytes) -> "tuple[dict, int]":
    """解析 Rigol :WAV:DATA? 内嵌头，返回 (缩放字段 dict, 数据起始偏移)。

    布局（小端，共 9 字节）：
      byte 0        : headless 标志（0/1）
      bytes 1-2     : Y origin（int16）
      bytes 3-4     : Y reference（int16）
      bytes 5-8     : Y increment（float32）
    换算：y = (raw - y_origin - y_reference) * y_increment
    """
    if len(payload) < 9:
        raise DecodeError(f"Rigol 头不足 9 字节（实际 {len(payload)}）")
    y_origin, y_ref = struct.unpack_from("<2h", payload, 1)
    (y_incr,) = struct.unpack_from("<f", payload, 5)
    return {
        "y_incr": float(y_incr),
        "y_origin": float(y_origin),
        "y_reference": int(y_ref),
    }, 9


# ---------------------------------------------------------------- 主入口

def decode_waveform(
    payload: bytes,
    opts: Optional[DecodeOptions] = None,
    channel: str = "CH1",
) -> Waveform:
    """把 CURVE? / :WAV:DATA? / WAVFrm? 的块内 payload 解码为 Waveform。

    payload 是去掉 #N<len> 头之后的原始字节。
    """
    opts = opts or DecodeOptions()
    opts.channel = channel
    data_offset = 0

    if opts.tds_header:
        scaling, data_offset = parse_tds_header(payload)
        merged = DecodeOptions(**{**opts.__dict__, **scaling})
        opts = merged

    if opts.rigol_header:
        scaling, data_offset = parse_rigol_header(payload)
        merged = DecodeOptions(**{**opts.__dict__, **scaling})
        opts = merged

    body = payload[data_offset:]
    raw = decode_raw(body, opts.encoding, opts.byte_order)

    if opts.rigol_header:
        # Rigol 换算：y = (raw - y_origin - y_reference) * y_increment
        y = (raw.astype(np.float64) - opts.y_origin - opts.y_reference) * opts.y_incr
    else:
        y = (raw.astype(np.float64) - opts.y_reference) * opts.y_incr + opts.y_origin

    x_start = opts.x_origin - opts.x_reference * opts.x_incr
    return Waveform(
        channel=channel,
        y=y,
        x_start=x_start,
        x_incr=opts.x_incr,
        raw=raw,
        source_encoding=opts.encoding.value,
    )


def decode_ascii_response(text: str, channel: str = "CH1", x_incr: float = 1e-9) -> Waveform:
    """解码 ASCII 波形文本（个别厂商 :WAV:DATA? ASC 模式）。"""
    parts = text.replace(",", " ").split()
    y = np.array([float(p) for p in parts], dtype=np.float64)
    return Waveform(channel=channel, y=y, x_incr=x_incr, source_encoding="ASCII")
