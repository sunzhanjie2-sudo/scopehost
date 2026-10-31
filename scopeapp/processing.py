"""非破坏性本地滤波、跨帧平均与数学通道。原始数据保持独立。"""
from __future__ import annotations
from collections import deque
import numpy as np
from .model import Waveform


def moving_average(wf: Waveform, samples: int) -> Waveform:
    samples = min(wf.points, max(1, int(samples)))
    if samples <= 1:
        return wf
    left = samples // 2
    padded = np.pad(wf.y, (left, samples - 1 - left), mode="edge")
    y = np.convolve(padded, np.ones(samples) / samples, mode="valid")
    return Waveform(wf.channel, y, wf.x_start, wf.x_incr,
                    decode_note=f"移动平均 {samples} 点")


def math_waveform(a: Waveform, b: Waveform, operation: str) -> Waveform:
    """在时间交集内插值运算；避免边界外推产生虚假的波形。"""
    if not a.points or not b.points:
        raise ValueError("数学运算需要两条非空波形")
    start, end = max(a.x_start, b.x_start), min(a.x[-1], b.x[-1])
    mask = (a.x >= start) & (a.x <= end)
    x, ay = a.x[mask], a.y[mask]
    if x.size < 2:
        raise ValueError("两个通道没有足够的共同采样时间")
    by = np.interp(x, b.x, b.y)
    if operation == "subtract":
        y = ay - by
    elif operation == "add":
        y = ay + by
    else:
        raise ValueError("数学运算支持加法或减法")
    return Waveform("MATH", y, float(x[0]), a.x_incr,
                    decode_note=f"{a.channel} {operation} {b.channel}")


class FrameAverager:
    def __init__(self) -> None:
        self.frames: dict[str, deque] = {}
        self.signatures: dict[str, tuple] = {}

    def clear(self) -> None:
        self.frames.clear()
        self.signatures.clear()

    def apply(self, waveforms: dict[str, Waveform], count: int) -> dict[str, Waveform]:
        if count <= 1:
            self.clear()
            return dict(waveforms)
        for name in set(self.frames) - set(waveforms):
            self.frames.pop(name, None)
            self.signatures.pop(name, None)
        result = {}
        for name, wf in waveforms.items():
            signature = (wf.points, wf.x_start, wf.x_incr, count)
            if self.signatures.get(name) != signature:
                self.frames[name] = deque(maxlen=count)
                self.signatures[name] = signature
            self.frames[name].append(wf.y.copy())
            y = np.mean(self.frames[name], axis=0)
            result[name] = Waveform(name, y, wf.x_start, wf.x_incr,
                                    decode_note=f"本地平均 {len(self.frames[name])}/{count} 帧")
        return result
