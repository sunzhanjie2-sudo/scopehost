"""深记录测量使用全部真实样点，迟滞边沿与噪声拒绝不逐候选遍历。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from scopeapp.measurements import _estimate_frequency, _schmitt_crossings, measure
from scopeapp.model import Waveform


class NoTimeArrayWaveform(Waveform):
    @property
    def x(self):
        raise AssertionError("均匀采样测量不能创建完整时间轴")


class NoFloatCopyCodes(np.ndarray):
    def astype(self, *args, **kwargs):
        raise AssertionError("量化码底噪判断不能复制为全量浮点数组")


class DeepMeasurementTests(unittest.TestCase):
    def test_uniform_interval_and_large_time_origin_need_no_time_array(self):
        dt = 1e-6
        samples = 7 + 2 * np.sin(np.arange(300_000) * 2 * np.pi * 1000 * dt)
        wf = NoTimeArrayWaveform(y=samples, x_start=-1e8, x_incr=dt)
        values = {m.name: m.value for m in measure(wf)}
        self.assertAlmostEqual(values["Freq"], 1000, places=6)
        self.assertAlmostEqual(values["Duty"], 50, places=5)
        self.assertAlmostEqual(values["Vpp"], 4, places=10)
        self.assertAlmostEqual(values["Vavg"], np.mean(samples), places=12)
        self.assertAlmostEqual(values["Vrms"], np.sqrt(np.mean(samples ** 2)), places=12)
        self.assertAlmostEqual(values["AC RMS"], np.std(samples), places=12)

    def test_ten_million_quantized_samples_skip_frequency_before_float_copy(self):
        raw = np.resize(np.array([100, 101, 102, 103], dtype=np.int16), 10_000_000)
        samples = raw.astype(np.float64) * .01
        raw = raw.view(NoFloatCopyCodes)
        wf = NoTimeArrayWaveform(y=samples, raw=raw, x_incr=4e-9)
        with patch("scopeapp.measurements._frequency_and_duty", side_effect=AssertionError("底噪无需做边沿分析")):
            values = {m.name: m.value for m in measure(wf)}
        self.assertEqual(values["Freq"], 0)
        self.assertEqual(values["Period"], 0)
        self.assertEqual(values["Duty"], 0)
        self.assertAlmostEqual(values["Vavg"], 1.015, places=12)
        self.assertAlmostEqual(values["Vpp"], .03, places=12)
        self.assertAlmostEqual(values["Vrms"], np.sqrt(np.mean(np.array([1., 1.01, 1.02, 1.03]) ** 2)), places=12)
        self.assertEqual(wf.points, 10_000_000)

    def test_signed_integer_extrema_cannot_overflow_noise_rejection(self):
        pattern = np.r_[np.full(50, -32768), np.full(50, 32767)].astype(np.int16)
        raw = np.tile(pattern, 200)
        wf = NoTimeArrayWaveform(y=raw.astype(np.float64) * .001, raw=raw, x_incr=1e-5)
        values = {m.name: m.value for m in measure(wf)}
        self.assertAlmostEqual(values["Freq"], 1000, places=6)
        self.assertAlmostEqual(values["Vpp"], 65.535, places=10)
        self.assertAlmostEqual(values["Duty"], 50, places=5)

    def test_millions_of_noise_candidates_are_rejected(self):
        # 有 250 万个候选上升沿，功能断言不依赖机器速度或计时阈值。
        samples = np.resize(np.array([-1., 1.]), 5_000_000)
        wf = NoTimeArrayWaveform(y=samples, x_incr=1e-6)
        values = {m.name: m.value for m in measure(wf)}
        self.assertEqual(values["Freq"], 0)
        self.assertEqual(values["Duty"], 0)
        self.assertEqual(values["Vavg"], 0)
        self.assertEqual(values["Vrms"], 1)

    def test_schmitt_edges_ignore_midpoint_chatter(self):
        pattern = np.r_[np.full(20, -1.), np.tile([.02, -.02], 10),
                        np.full(30, 1.), np.tile([-.02, .02], 10), np.full(30, -1.)]
        samples = np.tile(pattern, 100)
        rise, fall = _schmitt_crossings(samples, 0, .1)
        self.assertEqual(rise.size, 100)
        self.assertEqual(fall.size, 100)
        np.testing.assert_allclose(np.diff(rise), pattern.size, atol=1e-10)
        np.testing.assert_allclose(np.diff(fall), pattern.size, atol=1e-10)
        self.assertTrue(np.all(fall > rise))
        values = {m.name: m.value for m in measure(NoTimeArrayWaveform(y=samples, x_incr=1e-5))}
        self.assertAlmostEqual(values["Freq"], 1 / (pattern.size * 1e-5), places=7)

    def test_incomplete_cycles_keep_correct_duty(self):
        samples = np.tile(np.r_[np.full(30, 1.), np.full(70, -1.)], 500)[13:-21]
        values = {m.name: m.value for m in measure(NoTimeArrayWaveform(y=samples, x_incr=1e-5))}
        self.assertAlmostEqual(values["Freq"], 1000, places=6)
        self.assertAlmostEqual(values["Duty"], 30, places=5)

    def test_scattered_periods_do_not_claim_frequency(self):
        cycles = [np.r_[np.full(length // 2, -1.), np.full(length - length // 2, 1.)]
                  for length in (20, 100, 30, 80, 20, 200, 30, 90)]
        values = {m.name: m.value for m in measure(NoTimeArrayWaveform(y=np.concatenate(cycles), x_incr=.001))}
        self.assertEqual(values["Freq"], 0)
        self.assertEqual(values["Duty"], 0)

    def test_statistics_include_late_samples_outside_every_preview_prefix(self):
        samples = np.zeros(2_000_001)
        samples[-1] = 11
        values = {m.name: m.value for m in measure(NoTimeArrayWaveform(y=samples, x_incr=4e-9))}
        self.assertEqual(values["Vmax"], 11)
        self.assertEqual(values["Vmin"], 0)
        self.assertEqual(values["Vpp"], 11)
        self.assertAlmostEqual(values["Vavg"], 11 / samples.size, places=14)
        self.assertAlmostEqual(values["Vrms"], 11 / np.sqrt(samples.size), places=12)

    def test_invalid_and_constant_records_remain_honest(self):
        self.assertEqual(measure(Waveform()), [])
        self.assertEqual(measure(Waveform(y=np.array([1., np.nan]))), [])
        self.assertEqual(measure(Waveform(y=np.array([1., np.inf]))), [])
        values = {m.name: m.value for m in measure(NoTimeArrayWaveform(y=np.full(200, 7.), x_incr=1e-6))}
        self.assertEqual(values["Freq"], 0)
        self.assertEqual(values["Vpp"], 0)
        self.assertEqual(values["Vrms"], 7)
        self.assertEqual(values["AC RMS"], 0)

    def test_old_estimate_frequency_entry_point_accepts_uniform_axis(self):
        x = -.1 + np.arange(10000) * 1e-5
        samples = 8 + 2 * np.sin(np.arange(10000) * 2 * np.pi * .01)
        self.assertAlmostEqual(_estimate_frequency(x, samples), 1000, places=6)
        self.assertEqual(_estimate_frequency(np.array([0., 1.]), np.array([-1., 1.])), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
