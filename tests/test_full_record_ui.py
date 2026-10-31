"""完整记录 UI、后台 NPZ 快照及正常断开回归；离线夹具非实机验收。"""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from scopeapp.archive import load_archive, save_archive
from scopeapp.model import ChannelSettings, Measurement, TimebaseSettings, TriggerSettings, Waveform
from scopeapp.ui.main_window import MainWindow, wait_thread_responsive
from scopeapp.ui.worker import AcquisitionWorker
from scopeapp.waveform import sample_bounds, sample_value
from test_tektronix_full_record import fixture_adapter

APP = QApplication.instance() or QApplication([])


def pump(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    APP.processEvents()
    return bool(predicate())


def settings_for(wf):
    return {"channels": {f"CH{n}": ChannelSettings(number=n, enabled=n == 1)
                         for n in range(1, 5)},
            "timebase": TimebaseSettings(scale=wf.points * wf.x_incr / 10,
                                         record_length=wf.device_record_length,
                                         sample_rate=1 / wf.x_incr),
            "trigger": TriggerSettings()}


def payload_for(wf):
    return {"waveforms": {"CH1": wf}, "settings": settings_for(wf),
            "measurements": {"CH1": [Measurement("Vpp", float(np.ptp(wf.y)), "V")]},
            "acquisition_ms": 21000}


class CompleteRecordUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A five-million-point record with adjacent narrow pulse samples.
        raw = np.zeros(5_000_000, dtype=np.int16)
        raw[2_500_000:2_500_002] = [1234, -567]
        cls.waveform = Waveform("CH1", raw.astype(np.float64) * .002,
                                x_start=-.0025, x_incr=1e-9, raw=raw,
                                source_encoding="INT16", device_record_length=raw.size,
                                sample_stride=1, full_record=True)

    def setUp(self):
        self.errors = []
        self.warning = patch("scopeapp.ui.main_window.QMessageBox.warning",
                             side_effect=lambda *args: self.errors.append(args[-1]))
        self.warning.start()
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.path = Path(self.directory.name) / "capture.npz"
        self.window = MainWindow()
        self.window.show()
        payload = payload_for(self.waveform)
        with patch("scopeapp.ui.main_window.measure", side_effect=AssertionError("GUI 重算完整原始记录")):
            self.window._on_settings(payload["settings"])
            self.window._accept_frame(payload)
        APP.processEvents()

    def tearDown(self):
        writer = self.window._archive_writer
        if writer is not None and writer.isRunning():
            self.assertTrue(wait_thread_responsive(writer, timeout_ms=10000))
        self.window.close()
        APP.processEvents()
        self.warning.stop()
        self.directory.cleanup()

    def test_zoom_and_cursor_use_the_same_five_million_real_samples(self):
        window = self.window
        with patch("scopeapp.ui.main_window.measure", side_effect=AssertionError("ZOOM 不应重算完整记录")):
            window.btn_zoom.setChecked(True)
            window.spn_zoom_factor.setValue(50_000)
            window.spn_zoom_center.setValue(0)
            window._update_zoom()
        self.assertIs(window.zoom_view.waveforms["CH1"], self.waveform)
        self.assertIs(window.scope_view.waveforms["CH1"], self.waveform)
        self.assertEqual(window.zoom_view.waveforms["CH1"].points, 5_000_000)
        first, last = sample_bounds(self.waveform, *window.scope_view.zoom_range)
        self.assertIn(2_500_000, range(first, last))
        self.assertIn(2_500_001, range(first, last))
        self.assertIn(f"{last - first} 个采样点", window.lbl_zoom_info.text())
        self.assertEqual(sample_value(window.zoom_view.waveforms["CH1"], 0), 2.468)
        self.assertAlmostEqual(sample_value(window.zoom_view.waveforms["CH1"], 1e-9), -1.134, places=8)
        self.assertEqual(self.errors, [])

    def test_measurement_cache_is_used_for_unchanged_source_and_not_new_waveform(self):
        window = self.window
        with patch("scopeapp.ui.main_window.measure") as calculate:
            window._render_processed()
            calculate.assert_not_called()
            # Local processing yields a distinct waveform and must recompute its values.
            window.spn_filter.setValue(3)
            self.assertTrue(calculate.called)
        self.assertEqual(self.errors, [])

    def test_background_save_remains_responsive_and_keeps_original_frame_snapshot(self):
        started, release = threading.Event(), threading.Event()
        captured = []
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(15)

        def delayed_save(*args, **kwargs):
            captured.append(args)
            started.set()
            if not release.wait(6):
                raise RuntimeError("测试未释放归档写线程")
            return save_archive(*args, **kwargs)

        try:
            with patch("scopeapp.ui.main_window.save_archive", side_effect=delayed_save), \
                    patch.object(self.window, "_path", return_value=str(self.path)):
                self.window._save_archive()
                self.assertTrue(pump(lambda: started.is_set()))
                self.assertTrue(self.window._archive_writer.isRunning())
                self.assertTrue(pump(lambda: len(ticks) >= 10, 1))
                # A newly acquired shorter record replaces the UI dictionaries while
                # the writer still retains the previous immutable acquisition arrays.
                fresh_raw = np.arange(1000, dtype=np.int16)
                fresh = Waveform("CH1", fresh_raw.astype(float), x_incr=2e-9,
                                 raw=fresh_raw, device_record_length=1000,
                                 sample_stride=1, full_record=True)
                with patch("scopeapp.ui.main_window.measure", side_effect=AssertionError("GUI 重算新记录")):
                    self.window._accept_frame(payload_for(fresh))
                self.assertEqual(self.window._raw_waveforms["CH1"].points, 1000)
                self.assertIs(captured[0][1]["CH1"], self.waveform)
                self.assertEqual(captured[0][2]["timebase"].record_length, 5_000_000)
                self.assertAlmostEqual(captured[0][2]["timebase"].sample_rate, 1e9, delta=1e-4)
                release.set()
                self.assertTrue(pump(lambda: self.window._archive_writer is None, 8))
            restored, settings, metadata = load_archive(self.path)
            self.assertEqual(restored["CH1"].points, 5_000_000)
            self.assertEqual(settings["timebase"].record_length, 5_000_000)
            self.assertEqual(metadata["acquisition"]["completeness"], "complete")
            np.testing.assert_array_equal(restored["CH1"].raw, self.waveform.raw)
            np.testing.assert_array_equal(restored["CH1"].y, self.waveform.y)
            self.assertLess(max(np.diff(ticks)), .35)
            self.assertEqual(self.errors, [])
        finally:
            release.set()
            timer.stop()

    def test_close_waits_for_writer_without_freezing_gui(self):
        started, release = threading.Event(), threading.Event()
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(15)

        def delayed_save(*args, **kwargs):
            started.set()
            if not release.wait(6):
                raise RuntimeError("测试未释放归档写线程")
            return save_archive(*args, **kwargs)

        try:
            with patch("scopeapp.ui.main_window.save_archive", side_effect=delayed_save), \
                    patch.object(self.window, "_path", return_value=str(self.path)):
                self.window._save_archive()
                self.assertTrue(pump(lambda: started.is_set()))
                QTimer.singleShot(200, release.set)
                before = time.monotonic()
                self.assertTrue(self.window.close())
                self.assertGreater(time.monotonic() - before, .15)
                APP.processEvents()
            self.assertTrue(self.path.exists())
            self.assertGreater(len(ticks), 8)
            self.assertFalse(self.window.isVisible())
            self.assertEqual(self.errors, [])
        finally:
            release.set()
            timer.stop()

    def test_preview_cannot_replace_an_existing_npz(self):
        window = self.window
        preview = Waveform("CH1", self.waveform.y[::50000].copy(),
                           x_start=self.waveform.x_start, x_incr=50000e-9,
                           raw=self.waveform.raw[::50000].copy(),
                           device_record_length=5_000_000, sample_stride=50000,
                           full_record=False)
        preview_payload = {"waveforms": {"CH1": preview}, "settings": settings_for(self.waveform),
                           "measurements": {"CH1": []}}
        window._accept_frame(preview_payload)
        original = b"preserved prior archive"
        self.path.write_bytes(original)
        with patch.object(window, "_path", return_value=str(self.path)):
            window._save_archive()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(self.errors), 1)
        self.assertIn("100", self.errors[0])
        self.assertIn("5000000", self.errors[0])

    def test_live_save_requests_manual_stop_without_controlling_the_device(self):
        live = replace(self.waveform, record_stable=False)
        self.window._accept_frame(payload_for(live))
        original = b"preserved prior archive"
        self.path.write_bytes(original)
        with patch.object(self.window, "_path") as choose_path, \
                patch.object(self.window, "_submit") as device_command:
            self.window._save_archive()
            choose_path.assert_not_called()
            device_command.assert_not_called()
        self.assertIsNone(self.window._archive_writer)
        self.assertIs(self.window._raw_waveforms["CH1"], live)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(self.errors), 1)
        self.assertIn("手动 STOP", self.errors[0])
        self.assertIn("不会自动切换", self.errors[0])


class CompleteRecordWorkerTests(unittest.TestCase):
    def test_disconnect_finishes_current_block_without_changing_acquisition_state(self):
        adapter, fixture = fixture_adapter(record=1001)
        current_block, release = threading.Event(), threading.Event()
        original_read = fixture.read_raw
        original_guard = adapter.engine.before_operation
        old_progress, old_snapshot = [], []
        progress_callback = old_progress.append
        snapshot_callback = lambda: old_snapshot.append("unused")
        adapter.waveform_progress = progress_callback
        adapter.frame_settings_callback = snapshot_callback

        def delayed_read():
            if isinstance(fixture.result, bytes):
                current_block.set()
                if not release.wait(4):
                    raise RuntimeError("测试未释放当前波形块")
            return original_read()

        adapter.read_channel = lambda n: ChannelSettings(number=n, enabled=n <= 2)
        adapter.read_timebase = lambda: TimebaseSettings(record_length=fixture.record, sample_rate=5e8)
        adapter.read_trigger = lambda: TriggerSettings()
        initial = {"channels": {f"CH{n}": adapter.read_channel(n) for n in range(1, 5)},
                   "timebase": adapter.read_timebase(), "trigger": adapter.read_trigger()}
        worker = AcquisitionWorker(adapter, initial_settings=initial,
                                   settings_interval_ms=999999, wait_for_display=True)
        frames, errors, ticks = [], [], []
        worker.dataReady.connect(frames.append)
        worker.error.connect(errors.append)
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(15)
        try:
            with patch.object(fixture, "read_raw", side_effect=delayed_read):
                worker.start()
                self.assertTrue(pump(lambda: current_block.is_set()))
                self.assertFalse(fixture.running, "用户停止的采集保持停止")
                worker.request_stop()
                QTimer.singleShot(150, release.set)
                self.assertTrue(wait_thread_responsive(worker, timeout_ms=4000))
                APP.processEvents()
            self.assertFalse(fixture.running, "断开不得重新启动采集")
            self.assertFalse(any("ACQUIRE:STATE " in cmd.upper() for cmd in fixture.commands))
            self.assertEqual(fixture.curve_count, 1, "完成当前块后禁止启动后续块")
            self.assertFalse(fixture.header)
            self.assertEqual(frames, [], "部分记录不得发给 GUI")
            self.assertEqual(errors, [], "正常停止不得报告采集故障")
            self.assertIs(adapter.engine.before_operation, original_guard)
            self.assertIs(adapter.waveform_progress, progress_callback)
            self.assertIs(adapter.frame_settings_callback, snapshot_callback)
            self.assertGreater(len(ticks), 5)
        finally:
            release.set()
            worker.request_stop()
            self.assertTrue(wait_thread_responsive(worker, timeout_ms=4000))
            timer.stop()


if __name__ == "__main__":
    (ROOT / "tmp").mkdir(exist_ok=True)
    unittest.main(verbosity=2)
