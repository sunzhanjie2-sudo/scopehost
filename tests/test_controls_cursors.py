"""数字编辑、光标和整窗截图的交互回归，使用保存的真实 DS1152E 采样。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTabWidget
from scopeapp.ui.main_window import MainWindow
from scopeapp.archive import load_archive
from complete_snapshot_fixture import install_complete_snapshot

APP = QApplication.instance() or QApplication([])
SLOT_ERRORS = []
sys.excepthook = lambda kind, value, trace: SLOT_ERRORS.append(str(value))


class ControlsCursorTests(unittest.TestCase):
    def setUp(self):
        self.warnings = []
        warning = patch("scopeapp.ui.main_window.QMessageBox.warning",
                        side_effect=lambda *args: self.warnings.append(args[-1]))
        warning.start()
        self.addCleanup(warning.stop)
        self.w = MainWindow()
        self.w.show()
        with patch("scopeapp.ui.main_window.QFileDialog.getOpenFileName",
                   return_value=(str(ROOT / "artifacts/ds1152e_real_capture.npz"), "")):
            self.w._open_archive()
        self.w._submit = Mock()
        APP.processEvents()

    def tearDown(self):
        self.w.close()
        APP.processEvents()
        self.assertFalse(SLOT_ERRORS, str(SLOT_ERRORS))
        self.assertFalse(self.warnings, str(self.warnings))

    def test_number_edit_survives_live_settings_refresh_and_enter_commits(self):
        w = self.w
        w.findChild(QTabWidget).setCurrentIndex(1)
        w._ch_widgets[1]["group"].setEnabled(True)
        control = w._ch_widgets[1]["offset"]
        control.setFocus()
        control.lineEdit().selectAll()
        QTest.keyClicks(control, "0.75")
        w._sync_controls(w._settings["channels"], w._settings["timebase"], w._settings["trigger"])
        self.assertEqual(control.text(), "0.75")
        QTest.keyClick(control, Qt.Key_Return)
        self.assertAlmostEqual(control.value(), .75)
        w._submit.assert_called_with("set_channel_offset", 1, .75)
        self.assertFalse(control.editing)

    def test_stale_readback_waits_for_command_and_then_uses_actual_device_value(self):
        w = self.w
        del w._submit
        real_session = w.session
        fake_worker = Mock()
        w.session = SimpleNamespace(connected=True)
        w.worker = fake_worker
        try:
            control = w._ch_widgets[1]["offset"]
            control.setValue(.75)
            fake_worker.submit.assert_called_with("set_channel_offset", 1, .75)
            w._sync_controls(w._settings["channels"], w._settings["timebase"], w._settings["trigger"])
            self.assertEqual(control.value(), .75)
            w._command_finished({"name": "set_channel_offset", "args": (1, .75), "result": None})
            channels = dict(w._settings["channels"])
            channels["CH1"] = replace(channels["CH1"], offset=.76)
            w._sync_controls(channels, w._settings["timebase"], w._settings["trigger"])
            self.assertEqual(control.value(), .76)
        finally:
            w.worker = None
            w.session = real_session

    def test_left_minus_and_right_plus_use_channel_and_timebase_steps(self):
        w = self.w
        for n in (1, 2):
            w._ch_widgets[n]["group"].setEnabled(True)
            control = w._ch_widgets[n]["offset"]
            before = control.value()
            step = w._settings["channels"][f"CH{n}"].scale / 5
            QTest.mouseClick(control.minus_button, Qt.LeftButton)
            self.assertAlmostEqual(control.value(), before - step)
            QTest.mouseClick(control.plus_button, Qt.LeftButton)
            self.assertAlmostEqual(control.value(), before)
        before = w.spn_offset.value()
        step = w.scope_view.timebase.scale / 5
        QTest.mouseClick(w.spn_offset.minus_button, Qt.LeftButton)
        self.assertAlmostEqual(w.scope_view.timebase.position, before - step)
        QTest.mouseClick(w.spn_offset.plus_button, Qt.LeftButton)
        self.assertAlmostEqual(w.scope_view.timebase.position, before)

    def test_vertical_cursors_show_each_channel_and_no_extrapolation(self):
        w = self.w
        w.btn_cursor.click()
        w.cmb_cursor_scope.setCurrentIndex(1)
        view = w.scope_view
        view.cursor_times = [-.0003, .0004]
        values = view.cursor_values()
        self.assertEqual(set(values["channels"]), {"CH1", "CH2"})
        for name, readings in values["channels"].items():
            expected = np.interp(view.cursor_times, view.waveforms[name].x, view.waveforms[name].y)
            self.assertAlmostEqual(readings["va"], expected[0])
            self.assertAlmostEqual(readings["vb"], expected[1])
            self.assertAlmostEqual(readings["dv"], expected[1] - expected[0])
        w._cursor_readout(values)
        self.assertIn("CH1：A", w.lbl_cursor.text())
        self.assertIn("CH2：A", w.lbl_cursor.text())
        view.cursor_times[0] = -100
        values = view.cursor_values()
        self.assertIsNone(values["channels"]["CH1"]["va"])
        self.assertIsNotNone(values["channels"]["CH1"]["vb"])
        self.assertIsNone(values["channels"]["CH1"]["dv"])
        w.cmb_cursor_scope.setCurrentIndex(0)
        self.assertEqual(set(view.cursor_values()["channels"]), {"CH1"})

    def test_horizontal_cursor_drag_in_zoom_stack_uses_selected_channel_voltage(self):
        w = self.w
        w.btn_zoom.click()
        w.btn_stacked.click()
        w.btn_cursor.click()
        w.cmb_cursor_source.setCurrentText("CH2")
        w.cmb_cursor_mode.setCurrentIndex(2)
        w.cmb_zoom_voltage.setCurrentIndex(1)
        APP.processEvents()
        view = w.zoom_view
        ch = view.channels["CH2"]
        x0, top, width, height = view._lane_geometry("CH2")
        x = int(x0 + width * .7)
        y = int(view._volts_to_px(view.cursor_voltages[0], ch, "CH2"))
        target = int(top + height * .6)
        QTest.mousePress(view, Qt.LeftButton, pos=QPoint(x, y))
        QTest.mouseMove(view, QPoint(x, target))
        QTest.mouseRelease(view, Qt.LeftButton, pos=QPoint(x, target))
        self.assertAlmostEqual(view.cursor_voltages[0], view._px_to_volts(target, ch, "CH2"))
        self.assertEqual(view.cursor_voltages, w.scope_view.cursor_voltages)
        self.assertIn("水平光标 CH2", w.lbl_cursor.text())
        self.assertAlmostEqual(view.cursor_values()["voltage_delta"], view.cursor_voltages[1] - view.cursor_voltages[0])
        w._submit.assert_not_called()

    def test_bandwidth_display_uses_readback_and_keeps_unknown_archive_honest(self):
        w = self.w
        self.assertIn("CH1 未知", w.lbl_bandwidth.text())
        channels = dict(w._settings["channels"])
        channels["CH1"] = replace(channels["CH1"], bandwidth="20 MHz")
        channels["CH2"] = replace(channels["CH2"], bandwidth="FULL")
        w._sync_controls(channels, w._settings["timebase"], w._settings["trigger"])
        self.assertIn("CH1 20 MHz", w.lbl_bandwidth.text())
        self.assertIn("CH2 关闭（全带宽）", w.lbl_bandwidth.text())
        w._submit.assert_not_called()

    def test_save_image_contains_entire_application_not_only_plots(self):
        w = self.w
        w.btn_cursor.click()
        w.btn_zoom.click()
        APP.processEvents()
        expected = w.grab()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "window.png")
            with patch.object(w, "_path", return_value=path):
                w._save_plot()
            image = QImage(path)
            self.assertEqual(image.size(), expected.size())
            self.assertGreater(image.width(), w.plots.grab().width())
            self.assertEqual(image.pixelColor(20, 40), expected.toImage().pixelColor(20, 40))

    def test_archive_restores_cursor_modes_sources_and_positions(self):
        w = self.w
        install_complete_snapshot(w)
        w.btn_cursor.click()
        w.cmb_cursor_source.setCurrentText("CH2")
        w.cmb_cursor_scope.setCurrentIndex(1)
        w.cmb_cursor_mode.setCurrentIndex(2)
        w.scope_view.cursor_times = [-.0001, .0005]
        w.scope_view.cursor_voltages = [.015, -.01]
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "frame.npz")
            with patch.object(w, "_path", return_value=path):
                w._save_archive()
            waves, settings, metadata = load_archive(path)
            self.assertEqual(metadata["processing"]["cursor_voltages"], [.015, -.01])
            w.btn_cursor.click()
            with patch("scopeapp.ui.main_window.QFileDialog.getOpenFileName", return_value=(path, "")):
                w._open_archive()
            self.assertTrue(w.btn_cursor.isChecked())
            self.assertEqual(w.cmb_cursor_source.currentText(), "CH2")
            self.assertTrue(w.cmb_cursor_scope.currentData())
            self.assertEqual(w.scope_view.cursor_mode, "voltage")
            self.assertEqual(w.scope_view.cursor_times, [-.0001, .0005])
            self.assertEqual(w.zoom_view.cursor_voltages, [.015, -.01])
            np.testing.assert_array_equal(w._raw_waveforms["CH1"].y, waves["CH1"].y)


if __name__ == "__main__":
    unittest.main(verbosity=2)
