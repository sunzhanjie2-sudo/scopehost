"""本地分析与工作台集成回归：无需硬件，失败返回非零状态。"""
from __future__ import annotations
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from scopeapp.archive import save_archive, load_archive
from scopeapp.measurements import measure, fft_spectrum
from scopeapp.model import Waveform, ChannelSettings, TimebaseSettings, TriggerSettings
from scopeapp.processing import moving_average, math_waveform, FrameAverager
from scopeapp.ui.main_window import MainWindow
from scopeapp.ui.scope_view import _fmt_eng
from scopeapp.adapters.rigol import RigolAdapter
from scopeapp.model import ScopeIdentity

APP = QApplication.instance() or QApplication([])
SLOT_ERRORS = []
sys.excepthook = lambda kind, value, trace: SLOT_ERRORS.append(value)

def until(predicate, timeout=7):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        APP.processEvents()
        if predicate():
            return
        QTest.qWait(15)
    raise AssertionError("操作等待超时")

class AnalysisTests(unittest.TestCase):
    def test_rigol_unsigned_byte_without_private_header(self):
        class Engine:
            identity = ScopeIdentity("RIGOL", "DS1054Z")
            commands = []
            def write(self, command):
                self.commands.append(command)
            def ask(self, command):
                return "0,0,4,1,1e-6,-2e-6,0,0.02,3,128"
            def ask_binary(self, command):
                return b"#14" + bytes([0, 128, 131, 255])
        adapter = RigolAdapter(Engine())
        wf = adapter.fetch_waveform(1)
        np.testing.assert_allclose(wf.y, (np.array([0, 128, 131, 255]) - 131) * 0.02)
        self.assertEqual(wf.points, 4)
        self.assertEqual(wf.x_start, -2e-6)
        self.assertEqual(wf.raw.dtype, np.uint8)

    def test_duty_ignores_incomplete_cycles(self):
        t = np.arange(5500) / 100000
        y = np.where((t * 1000 + 0.1) % 1 < 0.3, 1.0, -1.0)
        values = {m.name: m.value for m in measure(Waveform("CH1", y, x_incr=1e-5))}
        self.assertAlmostEqual(values["Duty"], 30, delta=1)

    def test_dc_offset_frequency_and_window_amplitude(self):
        t = np.arange(10000) / 100000
        wf = Waveform("CH1", 7 + 2 * np.sin(2 * np.pi * 1000 * t), x_incr=1e-5)
        values = {m.name: m.value for m in measure(wf)}
        self.assertAlmostEqual(values["Freq"], 1000, delta=0.1)
        self.assertAlmostEqual(values["AC RMS"], np.sqrt(2), places=5)
        for window in ("hann", "hamming", "blackman", "rectangular"):
            f, db = fft_spectrum(wf, window)
            peak = np.argmax(db)
            self.assertAlmostEqual(f[peak], 1000)
            self.assertAlmostEqual(db[peak], 20 * np.log10(2), delta=0.005)

    def test_filter_and_time_overlap(self):
        wf = Waveform("CH1", np.ones(100) * 3, x_start=-0.5, x_incr=0.01)
        np.testing.assert_allclose(moving_average(wf, 20).y, 3)
        b = Waveform("CH2", np.ones(50), x_start=0, x_incr=0.01)
        result = math_waveform(wf, b, "subtract")
        self.assertEqual(result.points, 50)
        np.testing.assert_allclose(result.y, 2)
        self.assertEqual(wf.y[0], 3)

    def test_average_resets_on_timebase(self):
        average = FrameAverager()
        a = Waveform("CH1", np.ones(20), x_incr=1e-6)
        b = Waveform("CH1", np.ones(20) * 3, x_incr=1e-6)
        average.apply({"CH1": a}, 4)
        np.testing.assert_allclose(average.apply({"CH1": b}, 4)["CH1"].y, 2)
        b.x_incr = 2e-6
        np.testing.assert_allclose(average.apply({"CH1": b}, 4)["CH1"].y, 3)

    def test_archive_lossless_and_validation(self):
        wf = Waveform("CH1", np.linspace(-2, 3, 400), x_start=-2e-3, x_incr=1e-5)
        settings = {"channels": {"CH1": ChannelSettings()}, "timebase": TimebaseSettings(), "trigger": TriggerSettings()}
        with tempfile.TemporaryDirectory() as directory:
            path = save_archive(Path(directory) / "frame.npz", {"CH1": wf}, settings, "SIM")
            waves, restored, metadata = load_archive(path)
            np.testing.assert_array_equal(waves["CH1"].y, wf.y)
            self.assertEqual(waves["CH1"].x_start, wf.x_start)
            self.assertEqual(metadata["identity"], "SIM")
            self.assertEqual(restored["channels"]["CH1"].scale, 1)
            wf.x_incr = -1
            save_archive(path, {"CH1": wf}, settings)
            with self.assertRaises(ValueError):
                load_archive(path)

    def test_engineering_units(self):
        self.assertEqual(_fmt_eng(0.001, "s"), "1 ms")
        self.assertEqual(_fmt_eng(1000, "Hz"), "1 kHz")
        self.assertEqual(_fmt_eng(1e-6, "V"), "1 µV")

class WorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.window = MainWindow(auto_simulator=True)
        cls.window.show()
        until(lambda: cls.window._frames >= 2)

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        APP.processEvents()
        if SLOT_ERRORS:
            raise AssertionError(f"Qt 回调出现异常：{SLOT_ERRORS}")

    def test_01_controls_and_single_all_channels(self):
        w = self.window
        self.assertEqual(set(w._raw_waveforms), {"CH1", "CH2", "CH3", "CH4"})
        self.assertAlmostEqual(w._raw_waveforms["CH1"].vmean, 0, delta=0.05)
        w.cmb_timebase.setCurrentIndex(w.cmb_timebase.findData(500e-6))
        until(lambda: abs(w._settings["timebase"].scale - 500e-6) < 1e-12)
        w._on_position_changed(0.0001)
        QTest.qWait(2200)
        self.assertEqual(w.scope_view.timebase.position, 0.0001)
        w._ch_widgets[2]["coupling"].setCurrentText("GND")
        until(lambda: w._settings["channels"]["CH2"].coupling.value == "GND")
        until(lambda: w._raw_waveforms["CH2"].vpp == 0)
        w._ch_widgets[2]["coupling"].setCurrentText("DC")
        until(lambda: w._raw_waveforms["CH2"].vpp > 1)
        w._on_single()
        until(lambda: w._state == "SINGLE DONE")
        frames = w._frames
        QTest.qWait(500)
        self.assertEqual(w._frames, frames)
        self.assertEqual(len(w._raw_waveforms), 4)
        w._on_run_toggle()
        until(lambda: w._state == "RUN" and w._frames > frames)

    def test_02_impossible_trigger_force_and_cancel(self):
        w = self.window
        w.spn_trig_level.setValue(99)
        until(lambda: w._settings["trigger"].level == 99)
        w._on_single()
        until(lambda: w._state == "ARMED")
        QTest.qWait(350)
        self.assertEqual(w._state, "ARMED")
        w._submit("force_trigger")
        until(lambda: w._state == "SINGLE DONE")
        w._on_single()
        until(lambda: w._state == "ARMED")
        w._on_run_toggle()
        until(lambda: w._state == "STOP")
        w.spn_trig_level.setValue(0)
        w._on_run_toggle()
        until(lambda: w._state == "RUN")

    def test_03_analysis_export_and_replay(self):
        w = self.window
        w.btn_fft.setChecked(True)
        w.cmb_fft_span.setCurrentIndex(1)
        w.btn_cursor.setChecked(True)
        w.cmb_math.setCurrentIndex(1)
        w.spn_filter.setValue(11)
        until(lambda: "MATH" in w.scope_view.waveforms)
        self.assertTrue(w.fft_view.isVisible())
        self.assertIn("dv", w.scope_view.cursor_values())
        w.btn_reference.setChecked(True)
        self.assertEqual(len(w.scope_view.references), 5)
        # 靠近 A 游标拖动，不能改变时基平移。
        a = int(w.scope_view._time_to_px(w.scope_view.cursor_times[0]))
        before = w.scope_view.cursor_times[0]
        QTest.mousePress(w.scope_view, Qt.LeftButton, pos=QPoint(a, 80))
        QTest.mouseMove(w.scope_view, QPoint(a + 40, 80))
        QTest.mouseRelease(w.scope_view, Qt.LeftButton, pos=QPoint(a + 40, 80))
        self.assertNotEqual(w.scope_view.cursor_times[0], before)
        Path("artifacts").mkdir(exist_ok=True)
        self.assertTrue(w.grab().save("artifacts/workbench.png"))
        with tempfile.TemporaryDirectory() as directory:
            archive = str(Path(directory) / "frame.npz")
            with patch.object(w, "_path", return_value=archive):
                w._save_archive()
            for callback, filename in [(w._export_raw, "raw.csv"), (w._on_export_spreadsheet, "analysis.csv"), (w._export_fft, "fft.csv"), (w._export_measurements, "measure.csv")]:
                path = str(Path(directory) / filename)
                with patch.object(w, "_path", return_value=path):
                    callback()
                self.assertGreater(Path(path).stat().st_size, 20)
            with patch("scopeapp.ui.main_window.QFileDialog.getOpenFileName", return_value=(archive, "")):
                w._open_archive()
            self.assertEqual(w._state, "REPLAY")
            self.assertFalse(w.session.connected)
            self.assertEqual(len(w._raw_waveforms), 4)
            self.assertIn("MATH", w.scope_view.waveforms)
            w.cmb_timebase.setCurrentIndex(w.cmb_timebase.findData(200e-6))
            self.assertEqual(w.scope_view.timebase.scale, 200e-6)
            w._start_simulator_and_connect()
            until(lambda: w._state == "RUN")

if __name__ == "__main__":
    unittest.main(verbosity=2)
