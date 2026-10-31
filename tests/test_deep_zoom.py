"""完整深记录的局部绘图保持原始样点，绘图抽取不污染原始数据。"""
from pathlib import Path
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from PySide6.QtWidgets import QApplication
from scopeapp.model import ChannelSettings, TimebaseSettings, TriggerSettings, Waveform
from scopeapp.ui.scope_view import ScopeView
from scopeapp.waveform import display_sample_indices, sample_bounds, sample_value

APP = QApplication.instance() or QApplication([])


class NoTimeArrayWaveform(Waveform):
    @property
    def x(self):
        raise AssertionError("显示和游标不能构造完整时间轴")


def pump(predicate, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return bool(predicate())


class DeepZoomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        samples = np.zeros(10_000_000, dtype=np.float32)
        cls.peak = 5_000_003
        samples[cls.peak] = 7.5
        samples[cls.peak + 1] = -3.0
        cls.wave = NoTimeArrayWaveform("CH1", y=samples, x_start=-.02, x_incr=4e-9)

    def test_overview_preserves_single_sample_extrema_and_real_endpoints(self):
        wf = self.wave
        indices = display_sample_indices(wf, -.02, .02, 800)
        self.assertLessEqual(len(indices), 1602)
        self.assertEqual(indices[0], 0)
        self.assertEqual(indices[-1], wf.points - 1)
        self.assertIn(self.peak, indices)
        self.assertIn(self.peak + 1, indices)
        self.assertTrue(np.all(np.diff(indices) > 0))
        self.assertEqual(wf.points, 10_000_000)
        self.assertEqual(wf.y[self.peak], 7.5)

    def test_zoom_revisits_every_original_sample_around_narrow_pulse(self):
        wf = self.wave
        start = wf.x_start + (self.peak - 5) * wf.x_incr
        end = wf.x_start + (self.peak + 5) * wf.x_incr
        indices = display_sample_indices(wf, start, end, 800)
        np.testing.assert_array_equal(indices, np.arange(self.peak - 6, self.peak + 7))
        np.testing.assert_array_equal(wf.y[indices], wf.y[self.peak - 6:self.peak + 7])

    def test_index_arrays_are_limited_to_display_width(self):
        original_arange = np.arange
        with patch("scopeapp.waveform.np.arange", wraps=original_arange) as calls:
            indices = display_sample_indices(self.wave, -.02, .02, 640)
        self.assertLessEqual(len(indices), 1282)
        for call in calls.call_args_list:
            args = call.args
            length = args[0] if len(args) == 1 else args[1] - args[0]
            self.assertLessEqual(length, 1280)

    def test_bounds_match_actual_times_including_rounding_at_endpoints(self):
        wf = Waveform(y=np.arange(101), x_start=-.0037, x_incr=7e-6)
        actual_times = wf.x
        for a in (-2, 0, 1, 33, 99, 100, 102):
            for b in (a, a + .1, a + 2):
                start = wf.x_start + a * wf.x_incr
                end = wf.x_start + b * wf.x_incr
                expected = np.flatnonzero((actual_times >= start) & (actual_times <= end))
                first, stop = sample_bounds(wf, start, end)
                np.testing.assert_array_equal(np.arange(first, stop), expected)

    def test_empty_reversed_and_outside_ranges_are_honest(self):
        self.assertEqual(sample_bounds(Waveform(), 0, 1), (0, 0))
        self.assertEqual(sample_bounds(self.wave, 1, -1), (0, 0))
        self.assertEqual(sample_bounds(self.wave, -.05, -.03), (0, 0))
        self.assertEqual(sample_bounds(self.wave, .03, .04), (10_000_000, 10_000_000))
        self.assertEqual(sample_bounds(self.wave, float("nan"), 1), (0, 0))

    def test_cursor_reading_uses_adjacent_real_samples_without_time_array(self):
        wf = NoTimeArrayWaveform(y=np.array([0., 2., -1.]), x_start=-1, x_incr=.25)
        self.assertEqual(sample_value(wf, -1), 0)
        self.assertEqual(sample_value(wf, -.5), -1)
        self.assertEqual(sample_value(wf, -.875), 1)
        self.assertIsNone(sample_value(wf, -1.001))
        self.assertIsNone(sample_value(wf, -.499))
        view = ScopeView()
        view.waveforms = {"CH1": wf}
        view.cursor_times = [-.875, -.5]
        self.assertEqual(view.cursor_values()["channels"]["CH1"]["dv"], -2)
        view.close()

    def test_zoom_polygon_coordinates_are_original_samples_in_stacked_lane(self):
        wf = self.wave
        view = ScopeView()
        view.resize(900, 450)
        view.local_zoom = True
        view.set_stacked(True)
        ch = ChannelSettings(number=1, scale=2)
        center = wf.x_start + self.peak * wf.x_incr
        view.set_data({"CH1": wf, "CH2": wf}, {"CH1": ch, "CH2": ChannelSettings(number=2)},
                      TimebaseSettings(scale=wf.x_incr, position=center), TriggerSettings())
        x0, y0, gw, gh = view._geometry()
        indices = display_sample_indices(wf, center - 5 * wf.x_incr,
                                         center + 5 * wf.x_incr, gw)
        poly = view._wave_poly(wf, ch, x0, y0, gw, gh, "CH1")
        self.assertEqual(poly.size(), len(indices))
        self.assertLess(poly.size(), 20)
        for i, sample in enumerate(indices):
            self.assertAlmostEqual(poly[i].x(), view._time_to_px(wf.x_start + sample * wf.x_incr))
            self.assertAlmostEqual(poly[i].y(), view._volts_to_px(wf.y[sample], ch, "CH1"))
        view.close()

    def test_sparse_waveform_has_no_synthetic_points(self):
        wf = NoTimeArrayWaveform(y=np.array([0., 1., 0.]), x_start=-1, x_incr=1)
        view = ScopeView()
        view.set_data({"CH1": wf}, {"CH1": ChannelSettings()},
                      TimebaseSettings(scale=.2), TriggerSettings())
        poly = view._wave_poly(wf, ChannelSettings(), *view._geometry(), "CH1")
        self.assertEqual(poly.size(), 3)
        view.close()

    def test_deep_record_zoom_selection_can_reach_native_sample_interval(self):
        wf = self.wave
        view = ScopeView()
        view.set_data({"CH1": wf}, {"CH1": ChannelSettings()},
                      TimebaseSettings(scale=.004), TriggerSettings())
        center = wf.x_start + self.peak * wf.x_incr
        view._emit_zoom_range(center, center)
        self.assertAlmostEqual(view.zoom_range[1] - view.zoom_range[0], 2 * wf.x_incr)
        view.close()

    def test_small_fft_is_cached_until_window_or_source_changes(self):
        view = ScopeView()
        wf = Waveform(y=np.zeros(64))
        result = (np.arange(33), np.zeros(33))
        with patch("scopeapp.ui.scope_view.fft_spectrum", return_value=result) as compute:
            self.assertIs(view._fft_data(wf), result)
            self.assertIs(view._fft_data(wf), result)
            self.assertEqual(compute.call_count, 1)
            view.fft_window = "rectangular"
            self.assertIs(view._fft_data(wf), result)
            self.assertEqual(compute.call_count, 2)
            view._fft_data(Waveform(y=np.zeros(64)))
            self.assertEqual(compute.call_count, 3)
        view.close()

    def test_large_fft_uses_one_job_and_discards_superseded_result(self):
        view = ScopeView()
        original = Waveform(y=np.zeros(300_000))
        latest = Waveform(y=np.zeros(300_000))
        gate = threading.Event()
        result = (np.arange(4), np.zeros(4))

        def compute(wf, window):
            self.assertTrue(gate.wait(3))
            return result

        try:
            with patch("scopeapp.ui.scope_view.fft_spectrum", side_effect=compute) as compute_mock:
                view.waveforms = {"CH1": original}
                self.assertIsNone(view._fft_data(original))
                self.assertTrue(view._fft_running)
                view.waveforms = {"CH1": latest}
                self.assertIsNone(view._fft_data(latest))
                self.assertEqual(compute_mock.call_count, 1)
                gate.set()
                self.assertTrue(pump(lambda: not view._fft_running))
                self.assertIsNone(view._fft_cache_waveform)
                self.assertIsNone(view._fft_data(latest))
                self.assertTrue(pump(lambda: not view._fft_running))
                self.assertIs(view._fft_cache_waveform, latest)
                self.assertIs(view._fft_data(latest), result)
                self.assertEqual(compute_mock.call_count, 2)
        finally:
            gate.set()
            view.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
