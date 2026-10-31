"""明确标注模拟来源的完整采样 UI 夹具，独立于历史设备预览归档。"""
from __future__ import annotations

import numpy as np

from scopeapp.model import ChannelSettings, TimebaseSettings, TriggerSettings, Waveform


def install_complete_snapshot(window):
    points, dt = 6000, 4e-7
    times = -.0012 + np.arange(points) * dt
    waveforms = {}
    for number, amplitude in ((1, 1.), (2, .02)):
        raw = np.rint(np.sin(2 * np.pi * 1000 * times) * amplitude / .001).astype(np.int16)
        waveforms[f"CH{number}"] = Waveform(
            f"CH{number}", raw.astype(np.float64) * .001, x_start=-.0012,
            x_incr=dt, raw=raw, source_encoding="INT16", device_record_length=points,
            sample_stride=1, full_record=True, decode_note="SIMULATION complete UI test fixture")
    settings = {"channels": {f"CH{number}": ChannelSettings(number=number, enabled=number <= 2,
                                                              scale=1. if number == 1 else .1)
                              for number in range(1, 5)},
                "timebase": TimebaseSettings(scale=points * dt / 10, position=0,
                                               record_length=points, sample_rate=1 / dt),
                "trigger": TriggerSettings()}
    window._source = "SIMULATION · complete UI fixture"
    window.lbl_status.setText(window._source)
    window._raw_waveforms = waveforms
    window._averaged_waveforms = dict(waveforms)
    window._settings = settings
    window._frame_settings = settings
    window._view_position = 0
    window._measurement_cache = {}
    window._sync_controls(settings["channels"], settings["timebase"], settings["trigger"])
    window._render_processed()
