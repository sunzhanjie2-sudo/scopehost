"""ZOOM/堆叠回归：使用已保存的 DS1152E 真实采样，检查局部交互与数据完整性。"""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from PySide6.QtCore import Qt, QPoint, QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from scopeapp.ui.main_window import MainWindow
from scopeapp.archive import save_archive, load_archive
from complete_snapshot_fixture import install_complete_snapshot

APP = QApplication.instance() or QApplication([])
SLOT_ERRORS = []
sys.excepthook = lambda kind, value, trace: SLOT_ERRORS.append(str(value))
REAL_CAPTURE = ROOT / "artifacts/ds1152e_real_capture.npz"

class ZoomStackedTests(unittest.TestCase):
    def setUp(self):
        self.warnings = []
        warning = patch("scopeapp.ui.main_window.QMessageBox.warning",
                        side_effect=lambda *args: self.warnings.append(args[-1]))
        warning.start()
        self.addCleanup(warning.stop)
        self.window = MainWindow()
        self.window.show()
        with patch("scopeapp.ui.main_window.QFileDialog.getOpenFileName", return_value=(str(REAL_CAPTURE), "")):
            self.window._open_archive()
        APP.processEvents()
        self.window._submit = Mock()

    def tearDown(self):
        self.window.close()
        APP.processEvents()
        self.assertFalse(SLOT_ERRORS, str(SLOT_ERRORS))
        self.assertFalse(self.warnings, str(self.warnings))

    def test_01_zoom_is_local_and_keeps_raw_measurements(self):
        w=self.window
        before = {n: wf.y.copy() for n,wf in w._raw_waveforms.items()}
        measurements = {n: [(m.name,m.value) for m in items] for n,items in w._last_measurements.items()}
        original_scale = w.scope_view.timebase.scale
        w.btn_zoom.click()
        self.assertTrue(w.zoom_view.isVisible())
        self.assertAlmostEqual(w.zoom_view.timebase.scale, original_scale/4)
        self.assertIs(w.zoom_view.waveforms["CH1"], w.scope_view.waveforms["CH1"])
        self.assertAlmostEqual(w.scope_view.zoom_range[1]-w.scope_view.zoom_range[0], .0006)
        w.spn_zoom_factor.setValue(10)
        w.cmb_zoom_voltage.setCurrentIndex(2)
        self.assertEqual(w.scope_view.timebase.scale, original_scale)
        self.assertEqual(w.zoom_view.channels["CH1"].scale, w.scope_view.channels["CH1"].scale/5)
        for name,y in before.items(): np.testing.assert_array_equal(w._raw_waveforms[name].y,y)
        self.assertEqual(measurements,{n:[(m.name,m.value) for m in items] for n,items in w._last_measurements.items()})
        w._submit.assert_not_called()

    def test_02_move_resize_and_select_region(self):
        w=self.window;w.btn_zoom.click();APP.processEvents()
        view=w.scope_view
        x0,y0,gw,gh=view._geometry();y=int(y0+gh/2)
        a,b=view.zoom_range;middle=int(view._time_to_px((a+b)/2))
        QTest.mousePress(view,Qt.LeftButton,pos=QPoint(middle,y))
        QTest.mouseMove(view,QPoint(middle+50,y))
        QTest.mouseRelease(view,Qt.LeftButton,pos=QPoint(middle+50,y))
        moved=view.zoom_range
        self.assertGreater(sum(moved),a+b)
        self.assertAlmostEqual(moved[1]-moved[0],b-a,places=8)
        edge=int(view._time_to_px(moved[0]))
        QTest.mousePress(view,Qt.LeftButton,pos=QPoint(edge,y))
        QTest.mouseMove(view,QPoint(edge-40,y))
        QTest.mouseRelease(view,Qt.LeftButton,pos=QPoint(edge-40,y))
        self.assertGreater(view.zoom_range[1]-view.zoom_range[0],b-a)
        left=int(view._time_to_px(-.001));right=int(view._time_to_px(-.0007))
        QTest.mousePress(view,Qt.LeftButton,pos=QPoint(left,y))
        QTest.mouseMove(view,QPoint(right,y))
        QTest.mouseRelease(view,Qt.LeftButton,pos=QPoint(right,y))
        self.assertAlmostEqual(sum(view.zoom_range)/2,-.00085,delta=1e-5)
        self.assertAlmostEqual(w.zoom_view.timebase.position,sum(view.zoom_range)/2)
        w._submit.assert_not_called()

    def test_03_detail_wheel_pan_and_doubleclick_do_not_control_device(self):
        w=self.window;w.btn_zoom.click();APP.processEvents()
        view=w.zoom_view;original=w.scope_view.timebase.scale
        event=QWheelEvent(QPointF(400,80),QPointF(400,80),QPoint(),QPoint(0,120),Qt.NoButton,Qt.NoModifier,Qt.ScrollUpdate,False)
        APP.sendEvent(view,event)
        self.assertEqual(w.spn_zoom_factor.value(),8)
        QTest.mousePress(view,Qt.LeftButton,pos=QPoint(400,80))
        QTest.mouseMove(view,QPoint(450,80));QTest.mouseRelease(view,Qt.LeftButton,pos=QPoint(450,80))
        self.assertLess(w.spn_zoom_center.value(),0)
        QTest.mouseDClick(view,Qt.LeftButton,pos=QPoint(450,80))
        self.assertEqual(w.spn_zoom_factor.value(),4)
        self.assertEqual(w.spn_zoom_center.value(),w.scope_view.timebase.position)
        self.assertEqual(w.scope_view.timebase.scale,original)
        w._submit.assert_not_called()

    def test_04_stack_separates_channels_and_preserves_scales(self):
        w=self.window;w.btn_stacked.click();w.btn_zoom.click()
        for view in (w.scope_view,w.zoom_view):
            first=view._lane_geometry("CH1");second=view._lane_geometry("CH2")
            self.assertAlmostEqual(first[1]+first[3],second[1])
            for name in ("CH1","CH2"):
                ch=view.channels[name];_,top,_,height=view._lane_geometry(name)
                self.assertAlmostEqual(view._volts_to_px(ch.screen_center,ch,name),top+height/2)
                self.assertAlmostEqual(view._volts_to_px(ch.screen_center+ch.scale,ch,name),top+height/2-height/8)
        self.assertFalse(w._ch_widgets[3]["group"].isVisible())
        w.btn_fft.click();APP.processEvents()
        self.assertTrue(w.fft_view.isVisible())
        self.assertTrue(w.grab().save(str(ROOT/"artifacts/zoom_stacked_replay.png")))
        w._submit.assert_not_called()

    def test_05_cursor_coordinates_use_twelve_divisions_and_sync(self):
        w=self.window;w.btn_zoom.click();w.btn_cursor.click();APP.processEvents()
        view=w.zoom_view
        x0,y0,gw,gh=view._geometry()
        before=list(view.cursor_times)
        x=int(view._time_to_px(before[0]));target=int(x0+gw*.75)
        QTest.mousePress(view,Qt.LeftButton,pos=QPoint(x,int(y0+gh/2)))
        QTest.mouseMove(view,QPoint(target,int(y0+gh/2)))
        QTest.mouseRelease(view,Qt.LeftButton,pos=QPoint(target,int(y0+gh/2)))
        self.assertAlmostEqual(view.cursor_times[0],view._px_to_time(target))
        self.assertEqual(view.cursor_times,w.scope_view.cursor_times)
        self.assertIn("dv",view.cursor_values())
        self.assertAlmostEqual(w.scope_view._px_to_time(w.scope_view._geometry()[0]+w.scope_view._geometry()[2]),.0012)
        w._submit.assert_not_called()

    def test_06_zoom_export_contains_only_actual_samples(self):
        w=self.window;w.btn_zoom.click()
        start,end=w.scope_view.zoom_range
        result=w._zoom_waveforms()
        self.assertEqual(set(result),{"CH1","CH2"})
        for name,wf in result.items():
            original=w.scope_view.waveforms[name]
            mask=(original.x>=start)&(original.x<=end)
            np.testing.assert_array_equal(wf.y,original.y[mask])
            np.testing.assert_array_equal(wf.raw,original.raw[mask])
            self.assertLess(wf.points,original.points)
            self.assertGreaterEqual(wf.x_start,start)
            self.assertLessEqual(wf.x[-1],end)
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/"zoom.csv")
            with patch.object(w,"_path",return_value=path):w._export_zoom()
            data=np.loadtxt(path,delimiter=",",skiprows=1)
            self.assertEqual(data.shape[0],result["CH1"].points)

    def test_07_archive_restores_zoom_stack_settings(self):
        w=self.window;install_complete_snapshot(w);w.btn_zoom.click();w.btn_stacked.click()
        w.spn_zoom_factor.setValue(10);w.spn_zoom_center.setValue(.0004);w.cmb_zoom_voltage.setCurrentIndex(1)
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/"frame.npz")
            with patch.object(w,"_path",return_value=path):w._save_archive()
            waves,settings,meta=load_archive(path)
            self.assertTrue(meta["processing"]["stacked"])
            w.btn_zoom.click();w.btn_stacked.click();w.spn_zoom_factor.setValue(2)
            with patch("scopeapp.ui.main_window.QFileDialog.getOpenFileName",return_value=(path,"")):w._open_archive()
            self.assertTrue(w.btn_stacked.isChecked());self.assertTrue(w.btn_zoom.isChecked())
            self.assertEqual(w.spn_zoom_factor.value(),10)
            self.assertAlmostEqual(w.spn_zoom_center.value(),.0004)
            self.assertEqual(w.cmb_zoom_voltage.currentData(),2)
            np.testing.assert_array_equal(w._raw_waveforms["CH1"].y,waves["CH1"].y)

if __name__ == "__main__": unittest.main(verbosity=2)
