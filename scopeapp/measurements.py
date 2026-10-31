"""测量计算：波形统计 + 频率估计 + FFT 频谱。纯函数，可单测。"""
from __future__ import annotations

import numpy as np

from .model import Measurement, Waveform


_STAT_CHUNK = 1_000_000


def _run_starts(flags: np.ndarray) -> np.ndarray:
    """每段满足条件样点的起点；不在 Python 中逐采样或逐候选遍历。"""
    changes = np.empty(flags.size, dtype=bool)
    changes[0] = flags[0]
    np.logical_not(flags[:-1], out=changes[1:])
    np.logical_and(changes, flags, out=changes)
    return np.flatnonzero(changes)


def _confirmed_crossings(y: np.ndarray, threshold: float, target: np.ndarray,
                         opposite: np.ndarray, midpoint: np.ndarray) -> np.ndarray:
    """保留经过另一迟滞阈值后首次确认的边沿，返回真实交点的采样位置。"""
    if not target.size or not opposite.size or not midpoint.size:
        return np.empty(0)
    previous_opposite = np.searchsorted(opposite, target, side="left") - 1
    previous_target = np.r_[-1, target[:-1]]
    valid = (previous_opposite >= 0) & (
        opposite[np.maximum(previous_opposite, 0)] > previous_target)
    confirmed = target[valid]
    crossing_positions = np.searchsorted(midpoint, confirmed, side="left") - 1
    indices = midpoint[crossing_positions[crossing_positions >= 0]]
    before = np.asarray(y[indices], dtype=np.float64)
    after = np.asarray(y[indices + 1], dtype=np.float64)
    return indices + (threshold - before) / (after - before)


def _schmitt_crossings(y: np.ndarray, threshold: float,
                      hysteresis: float) -> tuple[np.ndarray, np.ndarray]:
    """全量向量化的迟滞上/下沿；中间抖动不会重复计作周期。"""
    lower = _run_starts(y < threshold - hysteresis)
    upper = _run_starts(y > threshold + hysteresis)
    rising_midpoints = _run_starts(y >= threshold)
    rising_midpoints = rising_midpoints[rising_midpoints > 0] - 1
    falling_midpoints = _run_starts(y < threshold)
    falling_midpoints = falling_midpoints[falling_midpoints > 0] - 1
    rise = _confirmed_crossings(y, threshold, upper, lower, rising_midpoints)
    fall = _confirmed_crossings(y, threshold, lower, upper, falling_midpoints)
    return rise, fall


def _frequency_and_duty(y: np.ndarray, dt: float, vmin: float, vmax: float) -> tuple[float, float]:
    if len(y) < 3 or dt <= 0 or not np.isfinite(dt) or vmax - vmin <= 1e-12:
        return 0.0, 0.0
    low, high = np.percentile(y, [5, 95])
    if high <= low:
        return 0.0, 0.0
    threshold = (low + high) / 2
    rise, fall = _schmitt_crossings(y, threshold, (high - low) * .05)
    if rise.size < 2:
        return 0.0, 0.0
    periods = np.diff(rise)
    period_samples = float(np.median(periods))
    # 极少采样点的周期或间隔散乱的噪声，不能给出可信频率。
    if period_samples < 8 or np.std(periods) > .2 * period_samples:
        return 0.0, 0.0
    duty = 0.0
    if fall.size:
        # 只测量具有一个下降沿的完整上升沿周期，避免逐周期扫描全部下降沿。
        first = np.searchsorted(fall, rise[:-1], side="right")
        stop = np.searchsorted(fall, rise[1:], side="left")
        valid = stop - first == 1
        if np.any(valid):
            ratios = (fall[first[valid]] - rise[:-1][valid]) / periods[valid]
            duty = float(np.median(ratios) * 100)
    return 1 / (period_samples * dt), duty


def _estimate_frequency(x: np.ndarray, y: np.ndarray) -> float:
    """兼容旧调用的均匀时间轴频率估计；正常测量直接使用采样间隔。"""
    if len(y) < 3 or len(x) != len(y):
        return 0.0
    dt = float((x[-1] - x[0]) / (len(x) - 1))
    return _frequency_and_duty(y, dt, float(np.min(y)), float(np.max(y)))[0]


def _statistics(y: np.ndarray):
    """统计覆盖全部真实样点，临时数组按块分配，适合千万点记录。"""
    minimum, maximum, total = np.inf, -np.inf, 0.0
    for start in range(0, y.size, _STAT_CHUNK):
        block = y[start:start + _STAT_CHUNK]
        if not np.all(np.isfinite(block)):
            return None
        minimum = min(minimum, float(np.min(block)))
        maximum = max(maximum, float(np.max(block)))
        total += float(np.sum(block, dtype=np.float64))
    mean = total / y.size
    squared_deviation = 0.0
    for start in range(0, y.size, _STAT_CHUNK):
        difference = np.asarray(y[start:start + _STAT_CHUNK], dtype=np.float64) - mean
        np.square(difference, out=difference)
        squared_deviation += float(np.sum(difference, dtype=np.float64))
    variance = squared_deviation / y.size
    return minimum, maximum, mean, float(np.sqrt(variance + mean * mean)), float(np.sqrt(variance))


def measure(wf: Waveform) -> list[Measurement]:
    """对单条波形计算常用测量项。"""
    y = wf.y
    if wf.points < 2:
        return []
    statistics = _statistics(y)
    if statistics is None:
        return []
    vmin, vmax, mean, rms, ac_rms = statistics
    quantized_noise = (wf.raw is not None and wf.raw.size > 0 and
                       np.issubdtype(wf.raw.dtype, np.integer) and
                       int(np.max(wf.raw)) - int(np.min(wf.raw)) <= 3)
    if quantized_noise:
        # 只有几个量化码的变化，保留电压统计但不宣称存在周期信号。
        freq, duty = 0.0, 0.0
    else:
        freq, duty = _frequency_and_duty(y, wf.x_incr, vmin, vmax)
    out = [
        Measurement("Vpp", vmax - vmin, "V"),
        Measurement("Vmax", vmax, "V"),
        Measurement("Vmin", vmin, "V"),
        Measurement("Vavg", mean, "V"),
        Measurement("Vrms", rms, "V"),
        Measurement("AC RMS", ac_rms, "V"),
        Measurement("Freq", freq, "Hz"),
        Measurement("Period", 1.0 / freq if freq > 0 else 0.0, "s"),
        Measurement("Duty", duty, "%"),
    ]
    return out


def fft_spectrum(wf: Waveform, window: str = "hann") -> tuple[np.ndarray, np.ndarray]:
    """单边峰值频谱 (Hz, dBV)，去直流，校正窗函数的相干增益。"""
    n = wf.points
    if n < 8 or wf.x_incr <= 0:
        return np.zeros(0), np.zeros(0)
    windows = {"hann": np.hanning, "hamming": np.hamming, "blackman": np.blackman,
               "rectangular": np.ones}
    if window not in windows:
        raise ValueError(f"未知 FFT 窗函数：{window}")
    win = windows[window](n)
    y = (wf.y - float(np.mean(wf.y))) * win
    spec = np.fft.rfft(y)
    mag = np.abs(spec) * 2.0 / np.sum(win)
    freqs = np.fft.rfftfreq(n, d=wf.x_incr)
    # 0Hz 分量不乘 2
    if mag.size > 0:
        mag[0] *= 0.5
    if n % 2 == 0:
        mag[-1] *= 0.5
    mag_db = 20.0 * np.log10(np.maximum(mag, 1e-12))
    return freqs, mag_db
