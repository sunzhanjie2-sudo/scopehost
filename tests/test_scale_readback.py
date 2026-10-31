"""时基及电压下拉框显示真实读回值，轮询同步不会发送设置命令。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import os
import sys
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from scopeapp.model import ChannelSettings, TimebaseSettings, TriggerSettings
from scopeapp.ui.main_window import MainWindow
from scopeapp.ui.scope_view import TIMEBASE_STEPS, VOLT_STEPS

APP = QApplication.instance() or QApplication([])


class ScaleReadbackTests(unittest.TestCase):
    def setUp(self):
        self.window = MainWindow()
        self.window.session = SimpleNamespace(connected=True, adapter=SimpleNamespace(channel_count=4),
                                              disconnect=Mock())
        self.window._submit = Mock()
        self.channels = {f"CH{n}": ChannelSettings(number=n) for n in range(1, 5)}
        self.timebase = TimebaseSettings()
        self.trigger = TriggerSettings()

    def tearDown(self):
        self.window.close()
        APP.processEvents()

    def sync(self, timebase=None, channels=None):
        self.window._sync_controls(channels or self.channels, timebase or self.timebase, self.trigger)

    def test_four_ms_readback_is_exact_and_does_not_select_five_ms(self):
        self.sync(timebase=replace(self.timebase, scale=.004))
        combo = self.window.cmb_timebase
        self.assertEqual(combo.currentData(), .004)
        self.assertEqual(combo.currentText(), "4 ms/div（设备读回）")
        self.assertEqual(combo.count(), len(TIMEBASE_STEPS) + 1)
        self.assertEqual(self.window.scope_view.timebase.scale, .004)
        self.window._submit.assert_not_called()

    def test_polling_updates_one_readback_item_and_standard_scale_removes_it(self):
        combo = self.window.cmb_timebase
        changed = Mock()
        combo.currentIndexChanged.connect(changed)
        for value in (.004, .008, .003, .004):
            self.sync(timebase=replace(self.timebase, scale=value))
            self.assertEqual(combo.currentData(), value)
            self.assertEqual(combo.count(), len(TIMEBASE_STEPS) + 1)
        self.sync(timebase=replace(self.timebase, scale=.005))
        self.assertEqual(combo.currentData(), .005)
        self.assertEqual(combo.currentText(), "5 ms/div")
        self.assertEqual(combo.count(), len(TIMEBASE_STEPS))
        self.assertEqual(combo.findData(True, Qt.ItemDataRole.UserRole + 1), -1)
        changed.assert_not_called()
        self.window._submit.assert_not_called()

    def test_nonpreset_voltage_retains_value_and_display_precision(self):
        combo = self.window._ch_widgets[1]["scale"]
        for value in (.123456789, .4, .123456789):
            channels = {**self.channels, "CH1": replace(self.channels["CH1"], scale=value)}
            self.sync(channels=channels)
            self.assertEqual(combo.currentData(), value)
            self.assertEqual(combo.count(), len(VOLT_STEPS) + 1)
        self.assertEqual(combo.currentText(), "123.456789 mV/div（设备读回）")
        self.assertEqual(self.window._ch_widgets[2]["scale"].currentData(), 1)
        self.sync(channels={**self.channels, "CH1": replace(self.channels["CH1"], scale=.5)})
        self.assertEqual(combo.currentData(), .5)
        self.assertEqual(combo.count(), len(VOLT_STEPS))
        self.window._submit.assert_not_called()

    def test_float_roundoff_matches_presets_without_absolute_tolerance(self):
        combo = self.window.cmb_timebase
        self.sync(timebase=replace(self.timebase, scale=.005 * (1 + 5e-11)))
        self.assertEqual(combo.currentData(), .005)
        self.assertEqual(combo.count(), len(TIMEBASE_STEPS))
        self.sync(timebase=replace(self.timebase, scale=4e-9))
        self.assertEqual(combo.currentData(), 4e-9, "纳秒时基不能被固定绝对容差误认为其它标准档")
        self.sync(timebase=replace(self.timebase, scale=2e-9 * (1 + 5e-11)))
        self.assertEqual(combo.currentData(), 2e-9)
        self.assertEqual(combo.count(), len(TIMEBASE_STEPS))
        self.sync(channels={**self.channels, "CH1": replace(self.channels["CH1"], scale=1 * (1 + 5e-11))})
        self.assertEqual(self.window._ch_widgets[1]["scale"].count(), len(VOLT_STEPS))
        self.window._submit.assert_not_called()

    def test_invalid_scale_does_not_add_entries_or_emit_setting(self):
        combo = self.window.cmb_timebase
        original = combo.currentData()
        for value in (float("nan"), float("inf"), -1, 0, None, "invalid"):
            self.window._set(combo, value)
            self.assertEqual(combo.currentData(), original)
            self.assertEqual(combo.count(), len(TIMEBASE_STEPS))
        self.window._submit.assert_not_called()

    def test_selecting_standard_preset_after_readback_sends_requested_value(self):
        self.sync(timebase=replace(self.timebase, scale=.004))
        combo = self.window.cmb_timebase
        combo.setCurrentIndex(combo.findData(.005))
        self.window._submit.assert_called_once_with("set_timebase", .005)
        self.assertEqual(combo.currentData(), .005)
        self.assertEqual(combo.count(), len(TIMEBASE_STEPS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
