"""示波器波形视图控件：网格、通道波形、触发线、FFT、缩放平移。"""
from __future__ import annotations

import math
import threading
from dataclasses import replace
from typing import Optional

from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget
import numpy as np

from ..model import ChannelSettings, TimebaseSettings, TriggerSettings, Waveform
from ..measurements import fft_spectrum
from ..waveform import display_sample_indices, sample_value
from .fonts import ensure_fonts

CHANNEL_COLORS = ["#FFD54A", "#4DD0E1", "#F06292", "#81C784", "#BA68C8", "#FF8A65"]

# 标准 1-2-5 时基档位（s/div）
TIMEBASE_STEPS = [1e-9, 2e-9, 5e-9, 1e-8, 2e-8, 5e-8, 1e-7, 2e-7, 5e-7,
                  1e-6, 2e-6, 5e-6, 1e-5, 2e-5, 5e-5, 1e-4, 2e-4, 5e-4,
                  1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 1e-1, 2e-1, 5e-1, 1.0]
VOLT_STEPS = [1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 1e-1, 2e-1, 5e-1, 1.0, 2.0, 5.0, 10.0]


def _fmt_eng(value: float, unit: str = "") -> str:
    """工程计数法格式化：1.00 ms / 2.50 V / 500 kHz。"""
    if not math.isfinite(value):
        return "—"
    if abs(value) < 1e-12:
        return f"0 {unit}".strip()
    prefixes = ["p", "n", "µ", "m", "", "k", "M", "G"]
    exp = math.floor(math.log10(abs(value) * (1 + 1e-12)))
    idx = min(len(prefixes) - 1, max(0, (exp // 3) + 4))
    scaled = value / (10 ** (3 * (idx - 4)))
    if abs(scaled) >= 999.5 and idx < len(prefixes) - 1:
        idx += 1
        scaled /= 1000
    return f"{scaled:.3g} {prefixes[idx]}{unit}".strip()


class ScopeView(QWidget):
    """示波器屏幕：10×8 格，多通道波形 + 可选 FFT 频谱带。"""

    timebaseChanged = Signal(float)
    positionChanged = Signal(float)
    cursorsChanged = Signal(object)
    zoomRangeChanged = Signal(float, float)
    zoomResetRequested = Signal()
    _fftReady = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        ensure_fonts()
        self.setMinimumSize(420, 200)
        self.setMouseTracking(True)
        self.waveforms: dict[str, Waveform] = {}
        self.channels: dict[str, ChannelSettings] = {}
        self.timebase = TimebaseSettings(scale=1e-3)
        self.trigger = TriggerSettings()
        self.fft_enabled = False
        self.fft_source = "CH1"
        self.fft_window = "hann"
        self.fft_span = 0.0
        self.cursor_enabled = False
        self.cursor_times = [-1e-3, 1e-3]
        self.cursor_source = "CH1"
        self.cursor_mode = "both"
        self.cursor_all_channels = False
        self.cursor_voltages = [1.0, -1.0]
        self._cursor_drag = None
        self.references: dict[str, Waveform] = {}
        self.reference_channels: dict[str, ChannelSettings] = {}
        self._drag_last_x: Optional[int] = None
        self._font = QFont("Microsoft YaHei UI", 9)
        self.data_source = ""
        self.stacked = False
        self.local_zoom = False
        self.zoom_range: Optional[tuple[float, float]] = None
        self._zoom_drag = None
        self._fft_cache_waveform = None
        self._fft_cache_window = None
        self._fft_cache = None
        self._fft_running = False
        self._fft_error = ""
        self._fftReady.connect(self._receive_fft)

    def set_stacked(self, on: bool) -> None:
        self.stacked = on
        self.update()

    def set_zoom_range(self, selected: Optional[tuple[float, float]]) -> None:
        self.zoom_range = selected
        self.update()

    def _lane_names(self) -> list[str]:
        names = list(dict.fromkeys([*self.waveforms, *self.references]))
        return [name for name in names if self.channels.get(name, self.reference_channels.get(name, ChannelSettings())).enabled]

    def _lane_geometry(self, name: Optional[str] = None) -> tuple[float, float, float, float]:
        x0, y0, gw, gh = self._geometry()
        names = self._lane_names()
        if self.stacked and name in names:
            height = gh / len(names)
            return x0, y0 + names.index(name) * height, gw, height
        return x0, y0, gw, gh

    def _px_to_time(self, x: float) -> float:
        x0, _, gw, _ = self._geometry()
        return self.timebase.position + ((x - x0) / gw - 0.5) * self.timebase.horizontal_divisions * self.timebase.scale

    def _emit_zoom_range(self, start: float, end: float) -> None:
        span = self.timebase.scale * self.timebase.horizontal_divisions
        left, right = self.timebase.position - span / 2, self.timebase.position + span / 2
        minimum = max((2 * w.x_incr for w in self.waveforms.values() if w.x_incr > 0),
                      default=span / 100)
        width = min(span, max(minimum, abs(end - start)))
        center = min(right - width / 2, max(left + width / 2, (start + end) / 2))
        self.zoom_range = (center - width / 2, center + width / 2)
        self.update()
        self.zoomRangeChanged.emit(*self.zoom_range)

    def set_cursors_enabled(self, on: bool) -> None:
        self.cursor_enabled = on
        if on:
            self.cursor_times = [self.timebase.position - self.timebase.scale,
                                 self.timebase.position + self.timebase.scale]
            self.reset_voltage_cursors()
        self.update()
        self.cursorsChanged.emit(self.cursor_values())

    def reset_voltage_cursors(self) -> None:
        ch = self.channels.get(self.cursor_source, ChannelSettings())
        self.cursor_voltages = [ch.screen_center + ch.scale, ch.screen_center - ch.scale]

    def cursor_values(self) -> dict:
        a, b = self.cursor_times
        dt = b - a
        c, d = self.cursor_voltages
        values = {"a": a, "b": b, "dt": dt, "frequency": 1 / abs(dt) if dt else 0,
                  "c": c, "d": d, "voltage_delta": d - c, "channels": {}}
        names = self._cursor_names()
        for name in names:
            wf = self.waveforms.get(name)
            readings = {"va": None, "vb": None, "dv": None}
            if wf is not None and wf.points:
                for key, t in (("va", a), ("vb", b)):
                    readings[key] = sample_value(wf, t)
                if readings["va"] is not None and readings["vb"] is not None:
                    readings["dv"] = readings["vb"] - readings["va"]
            values["channels"][name] = readings
        selected = values["channels"].get(self.cursor_source, {})
        values.update({key: value for key, value in selected.items() if value is not None})
        return values

    def _cursor_names(self) -> list[str]:
        if self.cursor_all_channels:
            return [name for name in self.waveforms if self.channels.get(name, ChannelSettings()).enabled]
        return [self.cursor_source] if self.cursor_source else []

    def set_fft_enabled(self, on: bool) -> None:
        self.fft_enabled = on
        self.update()

    # ---------------- 数据 ----------------
    def set_data(
        self,
        waveforms: dict[str, Waveform],
        channels: dict[str, ChannelSettings],
        timebase: TimebaseSettings,
        trigger: TriggerSettings,
    ) -> None:
        self.waveforms = waveforms
        self.channels = channels
        self.timebase = timebase
        self.trigger = trigger
        self.update()
        if self.cursor_enabled:
            self.cursorsChanged.emit(self.cursor_values())

    # ---------------- 渲染 ----------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        bg = QColor("#0b111b")
        p.fillRect(self.rect(), bg)

        if self.fft_enabled and self.waveforms:
            self._draw_fft_band(p)
            self._draw_annotations(p)
            return
        self._draw_time_grid(p)
        self._draw_waveforms(p)
        self._draw_trigger(p)
        self._draw_zoom_selection(p)
        self._draw_annotations(p)
        if self.cursor_enabled:
            self._draw_cursors(p)

    def _geometry(self) -> tuple[float, float, float, float]:
        w, h = self.width(), self.height()
        return 56.0, 30.0, max(1, w - 80.0), max(1, h - 80.0)

    def _px_per_div(self, gh: float) -> float:
        return gh / 8.0

    def _time_to_px(self, t: float) -> float:
        x0, _, gw, _ = self._geometry()
        half = gw / 2.0
        return x0 + half + (t - self.timebase.position) / self.timebase.scale * (gw / self.timebase.horizontal_divisions)

    def _volts_to_px(self, v: float, ch: ChannelSettings, name: Optional[str] = None) -> float:
        if name is None:
            name = next((key for key, value in self.channels.items() if value.number == ch.number), None)
        _, y0, _, gh = self._lane_geometry(name)
        center = y0 + gh / 2.0
        ppv = (gh / 2.0) / (4.0 * ch.scale)
        return center - (v - ch.screen_center) * ppv

    def _px_to_volts(self, y: float, ch: ChannelSettings, name: str) -> float:
        _, top, _, height = self._lane_geometry(name)
        return ch.screen_center + (top + height / 2 - y) * 8 * ch.scale / height

    def _draw_time_grid(self, p: QPainter) -> None:
        x0, y0, gw, gh = self._geometry()
        grid = QPen(QColor("#1E2933"), 1)
        axis = QPen(QColor("#3A4A57"), 1)
        p.setPen(grid)
        divisions = self.timebase.horizontal_divisions
        for i in range(divisions + 1):
            x = x0 + i * gw / divisions
            p.drawLine(int(x), int(y0), int(x), int(y0 + gh))
        lanes = self._lane_names() if self.stacked else [None]
        for name in lanes:
            _, top, _, height = self._lane_geometry(name)
            for j in range(9):
                y = top + j * height / 8.0
                p.setPen(axis if j in (0, 4, 8) else grid)
                p.drawLine(int(x0), int(y), int(x0 + gw), int(y))
            if name:
                ch = self.channels.get(name, self.reference_channels.get(name, ChannelSettings()))
                color_index = int(name[2:]) - 1 if name.startswith("CH") else 4
                p.setFont(self._font)
                p.setPen(QColor(CHANNEL_COLORS[color_index % len(CHANNEL_COLORS)]))
                p.drawText(int(x0 + 8), int(top + 3), 240, 18, Qt.AlignLeft,
                           f"{name}  {_fmt_eng(ch.scale, 'V')}/div")
                ticks = [(0, ch.screen_center + 4*ch.scale), (4, ch.screen_center), (8, ch.screen_center - 4*ch.scale)] if height >= 72 else [(4, ch.screen_center)]
                for j, v in ticks:
                    yy = top + j * height / 8
                    p.drawText(0, int(yy) - (16 if j == 8 else 8 if j == 4 else 0), int(x0 - 5), 16,
                               Qt.AlignRight, _fmt_eng(v, "V"))
        # 触发参考线（水平位置）
        p.setPen(QPen(QColor("#2E4A5A"), 1, Qt.DashLine))
        ref = self._time_to_px(0)
        if x0 <= ref <= x0 + gw:
            p.drawLine(int(ref), int(y0), int(ref), int(y0 + gh))
        # 时基标注
        p.setFont(self._font)
        p.setPen(QColor("#7A8B99"))
        t0 = self.timebase.position - divisions / 2 * self.timebase.scale
        relative = abs(self.timebase.position) > 20 * divisions * self.timebase.scale
        for i in range(divisions + 1):
            t = ((i - divisions / 2) * self.timebase.scale if relative
                 else t0 + i * self.timebase.scale)
            p.drawText(int(x0 + i * gw / divisions) - 30, int(y0 + gh + 14), 60, 16,
                       Qt.AlignCenter, _fmt_eng(t, "s"))
        p.drawText(int(x0), int(y0 + gh + 28), 140, 16,
                   Qt.AlignLeft, f"{_fmt_eng(self.timebase.scale, 's')}/div")

    def _draw_waveforms(self, p: QPainter) -> None:
        x0, y0, gw, gh = self._geometry()
        for name, wf in self.references.items():
            ch = self.reference_channels.get(name, ChannelSettings())
            if not self.channels.get(name, ch).enabled:
                continue
            p.save()
            p.setClipRect(QRectF(*self._lane_geometry(name)))
            p.setPen(QPen(QColor("#66778b"), 1, Qt.DotLine))
            p.drawPolyline(self._wave_poly(wf, ch, x0, y0, gw, gh, name))
            p.restore()
        for idx, (name, wf) in enumerate(self.waveforms.items()):
            ch = self.channels.get(name, ChannelSettings(number=idx + 1))
            if not ch.enabled:
                continue
            color_index = int(name[2:]) - 1 if name.startswith("CH") and name[2:].isdigit() else 4
            color = QColor(CHANNEL_COLORS[color_index % len(CHANNEL_COLORS)])
            p.save()
            p.setClipRect(QRectF(*self._lane_geometry(name)))
            p.setPen(QPen(color, 1.5))
            poly = self._wave_poly(wf, ch, x0, y0, gw, gh, name)
            if poly.size() > 1:
                p.drawPolyline(poly)
            p.restore()
        if self.stacked:
            p.setFont(self._font)
            p.setPen(QColor("#91a7bf"))
            p.drawText(int(x0), 4, 500, 18, Qt.AlignLeft,
                       "ZOOM 局部波形 · 通道堆叠" if self.local_zoom else "通道堆叠 · 共用时间轴")
            return
        for idx, (name, wf) in enumerate(self.waveforms.items()):
            ch = self.channels.get(name, ChannelSettings(number=idx + 1))
            color_index = int(name[2:]) - 1 if name.startswith("CH") and name[2:].isdigit() else 4
            color = QColor(CHANNEL_COLORS[color_index % len(CHANNEL_COLORS)])
            p.setFont(self._font)
            p.setPen(color)
            p.drawText(int(x0 + idx * 150), 5, 146, 18, Qt.AlignLeft,
                       f"{name}  {_fmt_eng(ch.scale, 'V')}/div")

    def _wave_poly(self, wf: Waveform, ch: ChannelSettings, x0, y0, gw, gh, name=None):
        """波形 → QPolygonF（批量绘制，比逐点 lineTo 快得多）。"""
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPolygonF

        if wf.points < 2:
            return QPolygonF()
        half_span = self.timebase.horizontal_divisions / 2 * self.timebase.scale
        indices = display_sample_indices(wf, self.timebase.position - half_span,
                                         self.timebase.position + half_span, gw)
        xs = wf.x_start + indices * wf.x_incr
        ys = wf.y[indices]
        _, top, _, height = self._lane_geometry(name)
        screen_x = x0 + gw / 2 + (xs - self.timebase.position) / self.timebase.scale * gw / self.timebase.horizontal_divisions
        screen_y = top + height / 2 - (ys - ch.screen_center) * height / (8 * ch.scale)
        pts = [QPointF(float(x), float(y)) for x, y in zip(screen_x, screen_y)]
        return QPolygonF(pts)

    def _draw_trigger(self, p: QPainter) -> None:
        src = self.trigger.source.upper()
        ch = self.channels.get(src)
        if ch is None:
            return
        x0, y0, gw, gh = self._lane_geometry(src)
        y = self._volts_to_px(self.trigger.level, ch, src)
        if y0 <= y <= y0 + gh:
            p.setPen(QPen(QColor("#FF5252"), 1, Qt.DashLine))
            p.drawLine(int(x0), int(y), int(x0 + gw), int(y))
            p.setFont(self._font)
            p.setPen(QColor("#FF8A80"))
            p.drawText(int(x0 + gw - 70), int(y) - 18, 70, 16, Qt.AlignRight,
                       f"{src} {self.trigger.level:g}V")

    def _draw_zoom_selection(self, p: QPainter) -> None:
        if self.zoom_range is None:
            return
        x0, y0, gw, gh = self._geometry()
        a, b = (min(x0+gw, max(x0, self._time_to_px(t))) for t in self.zoom_range)
        p.save()
        p.setClipRect(QRectF(x0, y0, gw, gh))
        p.fillRect(QRectF(a, y0, b-a, gh), QColor(75, 150, 255, 28))
        p.setPen(QPen(QColor("#70b0ff"), 2))
        p.drawRect(QRectF(a, y0+1, b-a, gh-2))
        for x in (a, b):
            p.fillRect(QRectF(x-3, y0+gh/2-10, 6, 20), QColor("#9ac8ff"))
        p.setFont(self._font)
        p.drawText(int(a+6), int(y0+5), max(1, int(b-a-12)), 18, Qt.AlignLeft, "ZOOM")
        p.restore()

    def _draw_annotations(self, p: QPainter) -> None:
        p.setFont(self._font)
        p.setPen(QColor("#55677A"))
        if self.fft_enabled:
            p.drawText(56, 4, self.width()-80, 18, Qt.AlignLeft, f"{self.data_source} · FFT")
            return
        annotation = self.data_source
        if abs(self.timebase.position) > 20 * self.timebase.horizontal_divisions * self.timebase.scale:
            annotation = f"相对中心时间 · 中心 t = {self.timebase.position:.12g} s"
        p.drawText(220, self.height() - 16, self.width() - 420, 16,
                   Qt.AlignCenter, annotation)
        status = ""
        if self.timebase.sample_rate and not self.fft_enabled:
            status = f"{_fmt_eng(self.timebase.sample_rate, 'Sa/s')}"
        p.drawText(int(self.width() - 180), int(self.height() - 16), 170, 16,
                   Qt.AlignRight, status)

    def _draw_fft_band(self, p: QPainter) -> None:
        """FFT 频谱带（30% 高度），显示第一个通道的频谱。"""
        wf = self.waveforms.get(self.fft_source)
        if wf is None:
            p.setPen(QColor("#7A8B99"))
            p.drawText(self.rect(), Qt.AlignCenter, "FFT 源通道暂无数据")
            return
        result = self._fft_data(wf)
        if result is None:
            p.setPen(QColor("#7A8B99"))
            p.drawText(self.rect(), Qt.AlignCenter, self._fft_error or "正在计算完整记录 FFT…")
            return
        freqs, mag = result
        if freqs.size < 2:
            return
        x0, y0, gw, gh = self._geometry()
        fh = gh
        bg = QColor("#0b111b")
        p.fillRect(self.rect(), bg)
        grid = QPen(QColor("#1E2933"), 1)
        p.setPen(grid)
        for i in range(11):
            x = x0 + i * gw / 10.0
            p.drawLine(int(x), int(y0), int(x), int(y0 + fh))
        p.setPen(QPen(QColor("#7A8B99"), 1.5))
        fmax = min(self.fft_span, float(freqs[-1])) if self.fft_span else float(freqs[-1])
        stop = int(np.searchsorted(freqs, fmax, side="right"))
        freqs, mag = freqs[:stop], mag[:stop]
        if freqs.size < 2 or fmax <= 0:
            return
        upper = np.ceil(np.max(mag) / 20) * 20
        lower = upper - 100
        for i in range(6):
            yy = y0 + i * fh / 5
            p.setPen(QColor("#1E2933"))
            p.drawLine(int(x0), int(yy), int(x0 + gw), int(yy))
            p.setPen(QColor("#7A8B99"))
            p.drawText(0, int(yy) - 8, 48, 16, Qt.AlignRight, f"{upper - i * 20:g}")
        pen = QPen(QColor("#4DD0E1"), 1.5)
        p.setPen(pen)
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPolygonF

        base = y0 + fh
        spectrum = Waveform(channel=wf.channel, y=mag, x_start=0,
                            x_incr=float(freqs[1] - freqs[0]))
        indices = display_sample_indices(spectrum, 0, fmax, gw)
        x = lambda f: x0 + (f / fmax) * gw  # noqa: E731
        y = lambda m: base - (max(lower, m) - lower) / (upper - lower) * fh  # noqa: E731
        pts = [QPointF(x(float(f)), y(float(m))) for f, m in zip(freqs[indices], mag[indices])]
        if len(pts) > 1:
            p.drawPolyline(QPolygonF(pts))
        p.setFont(self._font)
        p.setPen(QColor("#7A8B99"))
        p.drawText(int(x0), int(y0 + fh + 14), 300, 16, Qt.AlignLeft,
                   f"FFT {wf.channel} · {self.fft_window} · span {_fmt_eng(fmax, 'Hz')}")
        p.drawText(int(x0 + gw - 120), int(y0 + fh + 14), 120, 16, Qt.AlignRight, "dBV (峰值)")
        for i in range(6):
            p.drawText(int(x0 + i * gw / 5) - 35, int(y0 + fh + 30), 70, 16,
                       Qt.AlignCenter, _fmt_eng(fmax * i / 5, "Hz"))

    def _fft_data(self, wf: Waveform):
        """小记录复用 FFT；大记录最多单个后台任务，避免重绘阻塞。"""
        window = self.fft_window
        if self._fft_cache_waveform is wf and self._fft_cache_window == window:
            return self._fft_cache
        if wf.points < 262144:
            self._fft_cache = fft_spectrum(wf, window)
            self._fft_cache_waveform = wf
            self._fft_cache_window = window
            self._fft_error = ""
            return self._fft_cache
        if self._fft_running:
            return None
        self._fft_cache_waveform = None
        self._fft_cache_window = None
        self._fft_cache = None
        self._fft_running = True
        self._fft_error = ""

        def calculate():
            try:
                result, error = fft_spectrum(wf, window), ""
            except Exception as exc:
                result, error = None, f"FFT 计算失败：{exc}"
            try:
                self._fftReady.emit((wf, window, result, error))
            except RuntimeError:
                pass  # 窗口已经关闭并删除，后台结果不再提交。

        threading.Thread(target=calculate, daemon=True, name="ScopeHostFFT").start()
        return None

    def _receive_fft(self, payload) -> None:
        wf, window, result, error = payload
        self._fft_running = False
        if self.waveforms.get(self.fft_source) is wf and self.fft_window == window:
            self._fft_cache_waveform = wf
            self._fft_cache_window = window
            self._fft_cache = result
            self._fft_error = error
        self.update()

    def _draw_cursors(self, p: QPainter) -> None:
        x0, y0, gw, gh = self._geometry()
        p.setFont(self._font)
        if self.cursor_mode in ("time", "both"):
            for label, t in zip(("A", "B"), self.cursor_times):
                x = self._time_to_px(t)
                if x0 <= x <= x0 + gw:
                    p.setPen(QPen(QColor("#d6e1ee"), 1, Qt.DashLine))
                    p.drawLine(int(x), int(y0), int(x), int(y0 + gh))
                    p.drawText(int(x) + 5, int(y0) + 4, 24, 20, Qt.AlignLeft, label)
                    for name in self._cursor_names():
                        wf, ch = self.waveforms.get(name), self.channels.get(name)
                        if wf is None or ch is None or not ch.enabled:
                            continue
                        value = sample_value(wf, t)
                        if value is None:
                            continue
                        y = self._volts_to_px(value, ch, name)
                        _, top, _, height = self._lane_geometry(name)
                        if top <= y <= top + height:
                            index = ch.number - 1
                            p.setPen(QColor(CHANNEL_COLORS[index % len(CHANNEL_COLORS)]))
                            p.setBrush(QColor(CHANNEL_COLORS[index % len(CHANNEL_COLORS)]))
                            p.drawEllipse(QRectF(x - 3, y - 3, 6, 6))
                            p.setBrush(Qt.NoBrush)
        ch = self.channels.get(self.cursor_source)
        if self.cursor_mode in ("voltage", "both") and ch is not None and ch.enabled:
            x0, top, gw, height = self._lane_geometry(self.cursor_source)
            color = QColor(CHANNEL_COLORS[(ch.number - 1) % len(CHANNEL_COLORS)])
            for label, voltage in zip(("C", "D"), self.cursor_voltages):
                y = self._volts_to_px(voltage, ch, self.cursor_source)
                if top <= y <= top + height:
                    p.setPen(QPen(color, 1, Qt.DashDotLine))
                    p.drawLine(int(x0), int(y), int(x0 + gw), int(y))
                    p.drawText(int(x0 + 5), int(y) - 20, 230, 18, Qt.AlignLeft,
                               f"{label} {self.cursor_source} {_fmt_eng(voltage, 'V')}")

    # ---------------- 交互 ----------------
    def wheelEvent(self, event) -> None:  # noqa: N802
        if self.fft_enabled:
            return
        steps = event.angleDelta().y() / 120.0
        if steps == 0:
            return
        if self.local_zoom:
            new_scale = self.timebase.scale * 2 ** (-int(steps))
            self.timebaseChanged.emit(new_scale)
            return
        cur = self.timebase.scale
        idx = min(range(len(TIMEBASE_STEPS)),
                  key=lambda i: abs(TIMEBASE_STEPS[i] - cur))
        new_idx = max(0, min(len(TIMEBASE_STEPS) - 1, idx - int(steps)))
        new_scale = TIMEBASE_STEPS[new_idx]
        self.timebase = replace(self.timebase, scale=new_scale)
        self.update()
        self.timebaseChanged.emit(new_scale)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self.fft_enabled:
            return
        if event.button() == Qt.LeftButton:
            x0, y0, gw, gh = self._geometry()
            x, y = event.position().x(), event.position().y()
            if self.cursor_enabled and x0 <= x <= x0 + gw and y0 <= y <= y0 + gh:
                candidates = []
                if self.cursor_mode in ("time", "both"):
                    candidates += [(abs(self._time_to_px(t) - x), "time", i) for i, t in enumerate(self.cursor_times)]
                ch = self.channels.get(self.cursor_source)
                if self.cursor_mode in ("voltage", "both") and ch is not None and ch.enabled:
                    _, top, _, height = self._lane_geometry(self.cursor_source)
                    if top <= y <= top + height:
                        candidates += [(abs(self._volts_to_px(v, ch, self.cursor_source) - y), "voltage", i) for i, v in enumerate(self.cursor_voltages)]
                if candidates and min(candidates)[0] < 12:
                    _, kind, index = min(candidates)
                    self._cursor_drag = (kind, index)
                    return
            if self.zoom_range is not None and not event.modifiers() & Qt.AltModifier and x0 <= x <= x0+gw and y0 <= y <= y0+gh:
                a, b = self.zoom_range
                distances = [abs(self._time_to_px(t)-x) for t in (a,b)]
                mode = ("left" if distances[0] <= distances[1] else "right") if min(distances) < 10 else ("move" if a <= self._px_to_time(x) <= b else "select")
                self._zoom_drag = (mode, self._px_to_time(x), a, b)
                return
            self._drag_last_x = event.position().x()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._cursor_drag is not None:
            kind, index = self._cursor_drag
            if kind == "time":
                x0, _, gw, _ = self._geometry()
                self.cursor_times[index] = self._px_to_time(min(x0+gw, max(x0, event.position().x())))
            else:
                ch = self.channels[self.cursor_source]
                _, top, _, height = self._lane_geometry(self.cursor_source)
                self.cursor_voltages[index] = self._px_to_volts(min(top+height, max(top, event.position().y())), ch, self.cursor_source)
            self.update()
            self.cursorsChanged.emit(self.cursor_values())
            return
        if self._zoom_drag is not None:
            mode, anchor, a, b = self._zoom_drag
            t = self._px_to_time(event.position().x())
            if mode == "move":
                self._emit_zoom_range(a+t-anchor, b+t-anchor)
            elif mode == "left":
                self._emit_zoom_range(min(t,b), b)
            elif mode == "right":
                self._emit_zoom_range(a, max(t,a))
            else:
                self._emit_zoom_range(anchor, t)
            return
        if self._drag_last_x is not None:
            dx_px = event.position().x() - self._drag_last_x
            self._drag_last_x = event.position().x()
            _, _, gw, _ = self._geometry()
            dt = -dx_px / gw * self.timebase.horizontal_divisions * self.timebase.scale
            self.timebase = replace(self.timebase, position=self.timebase.position + dt)
            self.update()
            self.positionChanged.emit(self.timebase.position)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._zoom_drag is not None:
            mode, anchor, a, b = self._zoom_drag
            t = self._px_to_time(event.position().x())
            if mode == "select" and abs(t-anchor) < self.timebase.scale * .05:
                self._emit_zoom_range(t-(b-a)/2, t+(b-a)/2)
        self._zoom_drag = None
        self._drag_last_x = None
        self._cursor_drag = None

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self.local_zoom or self.zoom_range is not None:
            self.zoomResetRequested.emit()
            return
        self.positionChanged.emit(0.0)
        self.timebaseChanged.emit(1e-3)
