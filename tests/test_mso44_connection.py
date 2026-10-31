"""MSO44 官方命令夹具、VISA 挂起隔离与 GUI 响应回归（不代表真机验收）。"""
from __future__ import annotations
import math
import multiprocessing as mp
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from scopeapp.adapters.tektronix import TektronixAdapter, parse_wfmoutpre
from scopeapp.model import ChannelSettings, TimebaseSettings, TriggerSettings, Waveform
from scopeapp.scpi import ScpiEngine
from scopeapp.transport import Transport, TransportError, VisaTransport
from scopeapp.visa_process import VisaProcessTransport
from scopeapp.ui.main_window import MainWindow, ConnectDialog, ConnectionWorker, wait_thread_responsive
from scopeapp.ui.worker import AcquisitionWorker

APP = None


def pump_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.01)
    return False


def hanging_driver(pipe):
    pipe.recv()
    time.sleep(60)


def echo_driver(pipe):
    while True:
        operation, args = pipe.recv()
        if operation == "close":
            pipe.send((True, None))
            return
        if operation == "ask_binary":
            pipe.send((True, b"#14\x00\x0a\x0d\xff"))
        else:
            pipe.send((True, "TEKTRONIX,MSO44,SERIAL,FW" if operation == "ask" else None))


class Mso44Fixture(Transport):
    """只接受手册命令；旧 CHn:DISPLAY? / TRIG:A:LEVEL? / SYST:ERR? 均失败。"""
    def __init__(self, record=10_000_000, point_cap=10000):
        self.commands = []
        self.record, self.cap = record, point_cap
        self.header = False
        self.stride = 1
        self.source = "CH1"
        self.trigger_source = "CH2"
        self.enabled = {1: True, 2: False, 3: False, 4: False}
        self.closed = False
        self.result = None
        self.invalid_pre = False
        self.wrong_length = False
        self.horizontal_position = 50
        self.delay_mode = False
        self.delay_time = 0

    def connect(self):
        pass

    def disconnect(self):
        self.closed = True

    def write(self, command):
        self.commands.append(command)
        for part in command.upper().split(";"):
            head, _, arg = part.lstrip(":").partition(" ")
            if head == "*IDN?":
                self.result = "TEKTRONIX,MSO44,SERIAL,FW"
            elif head == "HEADER":
                self.header = arg in ("ON", "1")
            elif head == "VERBOSE":
                pass
            elif head == "HORIZONTAL:RECORDLENGTH?":
                self.result = str(self.record)
            elif head == "HORIZONTAL:SCALE?":
                self.result = ".001"
            elif head == "HORIZONTAL:SAMPLERATE?":
                self.result = "1e9"
            elif head == "HORIZONTAL:DIVISIONS?":
                self.result = "10"
            elif head == "HORIZONTAL:POSITION?":
                self.result = str(self.horizontal_position)
            elif head == "HORIZONTAL:DELAY:MODE?":
                self.result = str(int(self.delay_mode))
            elif head == "HORIZONTAL:DELAY:TIME?":
                self.result = str(self.delay_time)
            elif head == "ACQUIRE:STATE?":
                self.result = "1"
            elif head == "TRIGGER:A:EDGE:SOURCE?":
                self.result = self.trigger_source
            elif head == "TRIGGER:A:EDGE:SOURCE":
                self.trigger_source = arg
            elif head == "TRIGGER:A:LEVEL:CH2?":
                self.result = "1.25"
            elif head == "TRIGGER:A:LEVEL:CH2":
                pass
            elif head == "TRIGGER:A:EDGE:SLOPE?":
                self.result = "RISE"
            elif head == "TRIGGER:A:MODE?":
                self.result = "NORMAL"
            elif head.startswith("DISPLAY:GLOBAL:CH"):
                n = int(head.split(":")[2][2:])
                if head.endswith("?"):
                    self.result = str(int(self.enabled[n]))
                else:
                    self.enabled[n] = arg == "ON"
            elif head.startswith("CH") and ":" in head:
                _, _, setting = head.partition(":")
                answers = {"COUPLING?": "DC", "BANDWIDTH?": "2e7", "SCALE?": "1",
                           "OFFSET?": ".5", "POSITION?": "1.5"}
                if setting not in answers:
                    raise TransportError(f"夹具不支持 {part}")
                self.result = answers[setting]
            elif head == "DATA:SOURCE":
                self.source = arg
            elif head == "DATA:START":
                if arg != "1":
                    raise AssertionError("必须从记录起点覆盖预览")
            elif head == "DATA:STOP":
                if int(arg) != self.record:
                    raise AssertionError("不能只读记录开头的一小段")
            elif head == "DATA:RESAMPLE":
                self.stride = int(arg)
            elif head.startswith("WFMOUTPRE:") and arg:
                pass
            elif head == "WFMOUTPRE?":
                count = math.ceil(self.record / self.stride)
                fields = ["BYT_NR 2", "BIT_NR 16", "ENCDG BINARY", "BN_FMT RI", "BYT_OR MSB",
                          'WFID "Ch1, DC coupling; calibration"', f"NR_PT {count}", "PT_FMT Y",
                          "PT_ORDER LINEAR", 'XUNIT "s"', f"XINCR {self.stride / 1e9}",
                          "XZERO 1e-10", f"PT_OFF {self.record / 2 / self.stride}",
                          'YUNIT "V"', "YMULT .01", "YOFF .5", "YZERO .25"]
                if self.invalid_pre:
                    fields = [field for field in fields if not field.startswith("YMULT")]
                self.result = ":WFMOUTPRE:" + ";".join(fields)
            elif head == "CURVE?":
                if self.header:
                    raise AssertionError("读二进制前必须关闭响应头")
                count = math.ceil(self.record / self.stride) + int(self.wrong_length)
                codes = (np.arange(count) % 10).astype(">i2")
                data = codes.tobytes()
                length = str(len(data)).encode()
                self.result = b"#" + str(len(length)).encode() + length + data
            else:
                raise TransportError(f"手册夹具拒绝未知命令：{part}")

    def read_raw(self):
        if self.result is None:
            raise TransportError("设备无响应")
        return self.result if isinstance(self.result, bytes) else (self.result + "\n").encode()


def fixture_adapter(**kwargs):
    fixture = Mso44Fixture(**kwargs)
    engine = ScpiEngine(fixture)
    engine.connect()
    adapter = TektronixAdapter(engine)
    # These fixtures specifically exercise the optional preview protocol.
    adapter.set_preview_points(10000)
    return adapter, fixture


class ProtocolTests(unittest.TestCase):
    def test_hardware_horizontal_position_and_delay_use_seconds(self):
        adapter, fixture = fixture_adapter()
        fixture.horizontal_position = 75
        self.assertAlmostEqual(adapter.read_timebase().position, -.0025)
        fixture.delay_mode, fixture.delay_time = True, .002
        self.assertAlmostEqual(adapter.read_timebase().position, .002)

    def test_capture_preserves_existing_file_and_does_not_post_file_not_found(self):
        adapter, _ = fixture_adapter()
        png = b"\x89PNG\r\n\x1a\n" + b"image"
        files = {"C:/scopehost_capture.png": b"user file"}
        errors, created = [], []
        def write(command):
            path = command.split('"')[1]
            if command.upper().startswith(":SAVE:IMAGE"):
                files[path] = png
                created.append(path)
            elif path not in files:
                errors.append("File name not found")
            else:
                del files[path]
        def read(command):
            return files[command.split('"')[1]]
        with patch.object(adapter.engine, "write", side_effect=write), patch.object(adapter.engine, "ask", return_value="1"), patch.object(adapter.engine, "ask_binary", side_effect=read):
            self.assertEqual(adapter.capture_screen(), png)
            self.assertEqual(adapter.capture_screen(), png)
        self.assertEqual(files, {"C:/scopehost_capture.png": b"user file"})
        self.assertEqual(errors, [])
        self.assertEqual(len(set(created)), 2)

    def test_invalid_capture_is_rejected_and_temporary_file_is_cleaned(self):
        adapter, _ = fixture_adapter()
        files = set()
        def write(command):
            path = command.split('"')[1]
            if command.upper().startswith(":SAVE:IMAGE"):
                files.add(path)
            else:
                files.remove(path)
        with patch.object(adapter.engine, "write", side_effect=write), patch.object(adapter.engine, "ask", return_value="1"), patch.object(adapter.engine, "ask_binary", return_value=b"error message"):
            with self.assertRaisesRegex(RuntimeError, "PNG"):
                adapter.capture_screen()
        self.assertEqual(files, set())

    def test_actual_mso44_display_channel_trigger_and_position_commands(self):
        adapter, fixture = fixture_adapter()
        ch = adapter.read_channel(1)
        self.assertTrue(ch.enabled)
        self.assertEqual(ch.bandwidth, "20 MHz")
        self.assertEqual(ch.vertical_position, 1.5)
        self.assertFalse(adapter.read_channel(2).enabled)
        self.assertEqual(adapter.read_trigger().level, 1.25)
        adapter.set_trigger_level(.75)
        self.assertTrue(adapter.set_channel_enabled(2, True))
        self.assertIn(":TRIGger:A:LEVel:CH2 0.75", fixture.commands)
        self.assertFalse(any("SYST" in cmd.upper() or "CH1:DISPLAY" in cmd.upper() for cmd in fixture.commands))

    def test_long_record_is_bounded_and_covers_full_time_span(self):
        adapter, fixture = fixture_adapter()
        adapter.read_timebase()
        wf = adapter.fetch_waveform(1)
        self.assertEqual(wf.points, 10000)
        self.assertEqual(fixture.stride, 1000)
        self.assertAlmostEqual(wf.duration, .01)
        self.assertAlmostEqual(wf.x_start, -.005 + 1e-10)
        np.testing.assert_allclose(wf.y[:3], [.245, .255, .265])
        adapter.fetch_waveform(1)
        self.assertEqual(fixture.stride, 1000)
        self.assertFalse(any(cmd.upper().startswith(":ACQUIRE:") for cmd in fixture.commands))

    def test_preview_point_choice_applies_without_changing_record_depth(self):
        adapter, fixture = fixture_adapter(record=1234567)
        adapter.set_preview_points(20000)
        wf = adapter.fetch_waveform(1)
        self.assertLessEqual(wf.points, 20000)
        self.assertEqual(fixture.record, 1234567)
        self.assertEqual(fixture.stride, 62)
        self.assertLess(abs(wf.duration - fixture.record / 1e9), wf.x_incr)

    def test_missing_metadata_and_truncated_waveform_never_use_default_scaling(self):
        for key in ("invalid_pre", "wrong_length"):
            adapter, fixture = fixture_adapter()
            setattr(fixture, key, True)
            with self.assertRaises(ValueError):
                adapter.fetch_all()
            self.assertFalse(fixture.header)

    def test_header_off_and_fully_qualified_header_on_preambles(self):
        text = '2;16;BINARY;RI;MSB;"CH1; waveform";4;Y;LINEAR;"s";1e-6;0;1.5;"V";.02;.5;1'
        pre = parse_wfmoutpre(text)
        self.assertEqual(pre["XINCR"], "1e-6")
        self.assertEqual(pre["YOFF"], ".5")
        pre = parse_wfmoutpre(':WFMOUTPRE:BYT_NR 2;:WFMOUTPRE:XINCR 1e-6;YMULT .02')
        self.assertEqual(pre["XINCR"], "1e-6")

    def test_identity_timeout_cannot_become_unknown_connected_device(self):
        fixture = Mso44Fixture()
        fixture.write = lambda _: None
        engine = ScpiEngine(fixture)
        with self.assertRaises(TransportError):
            engine.connect()
        self.assertFalse(engine.connected)
        self.assertTrue(fixture.closed)

    def test_visa_binary_read_disables_newline_termination_and_restores_it(self):
        class Instrument:
            read_termination = "\n"
            def write(self, command):
                pass
            def read_raw(self):
                self.observed = self.read_termination
                return b"#14\x00\x0a\x0d\xff"
        transport = VisaTransport("TCPIP::host::INSTR")
        transport._inst = Instrument()
        self.assertEqual(len(transport.ask_binary("CURVE?")), 7)
        self.assertIsNone(transport._inst.observed)
        self.assertEqual(transport._inst.read_termination, "\n")


class AsyncTests(unittest.TestCase):
    def setUp(self):
        # 回归失败须报告错误，不能让离屏 QMessageBox 阻塞测试进程。
        self.dialog_errors = []
        warning = patch("scopeapp.ui.main_window.QMessageBox.warning", side_effect=lambda *args: self.dialog_errors.append(args[-1]))
        warning.start()
        self.addCleanup(warning.stop)

    def tearDown(self):
        self.assertEqual(self.dialog_errors, [])

    @classmethod
    def setUpClass(cls):
        global APP
        APP = QApplication.instance() or QApplication([])

    def test_hung_native_visa_driver_has_deadline_and_process_cleanup(self):
        transport = VisaProcessTransport()
        with patch("scopeapp.visa_process._visa_server", hanging_driver):
            transport._start()
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(TransportError, "超时"):
                transport._rpc("scan", deadline_ms=350)
            self.assertLess(time.monotonic() - started, 1.2)
            self.assertIsNone(transport._process)
        finally:
            transport.disconnect()

    def test_cancel_hung_native_call_and_binary_bytes_cross_process(self):
        transport = VisaProcessTransport()
        with patch("scopeapp.visa_process._visa_server", echo_driver):
            transport.connect()
        self.assertEqual(transport.ask_binary("CURVE?"), b"#14\x00\x0a\x0d\xff")
        transport.disconnect()
        with patch("scopeapp.visa_process._visa_server", hanging_driver):
            transport._start()
        threading.Timer(.15, transport.cancel).start()
        with self.assertRaisesRegex(TransportError, "取消"):
            transport._rpc("connect")
        self.assertIsNone(transport._process)

    def test_scan_dialog_keeps_gui_responsive_and_cancels_on_close(self):
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(20)
        with patch("scopeapp.visa_process._visa_server", hanging_driver):
            dialog = ConnectDialog()
            dialog.show()
            self.assertTrue(pump_until(lambda: len(ticks) >= 10))
            self.assertTrue(dialog._scanner.isRunning())
            dialog.visa_res.setEditText("USB0::manual::INSTR")
            dialog.reject()
        self.assertFalse(dialog._scanner.isRunning())
        self.assertEqual(dialog.selected()["visa"], "USB0::manual::INSTR")
        timer.stop()

    def test_connection_cancel_does_not_freeze_window_or_create_fake_waveform(self):
        window = MainWindow()
        window.show()
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(20)
        try:
            with patch("scopeapp.visa_process._visa_server", hanging_driver):
                window._do_connect({"method": 2, "visa": "USB0::test::INSTR", "adapter": None})
                self.assertTrue(pump_until(lambda: len(ticks) >= 8))
                self.assertEqual(window.btn_disconnect.text(), "取消连接")
                window.btn_disconnect.click()
                self.assertTrue(pump_until(lambda: not window.connector.isRunning()))
            self.assertFalse(window.session.connected)
            self.assertEqual(window._raw_waveforms, {})
            self.assertTrue(window.btn_connect.isEnabled())
            self.assertFalse(window.btn_disconnect.isEnabled())
        finally:
            timer.stop()
            window.close()

    def test_connection_preflight_checks_mso44_official_commands(self):
        fixture = Mso44Fixture()
        worker = ConnectionWorker({"method": 2, "visa": "USB0::fixture", "adapter": None})
        result, failures = [], []
        worker.connected.connect(result.append)
        worker.failed.connect(failures.append)
        with patch.object(worker.session, "connect_visa", side_effect=lambda *_: worker.session._connect(fixture, None)):
            worker.start()
            self.assertTrue(pump_until(lambda: not worker.isRunning()))
        self.assertEqual(failures, [])
        self.assertTrue(result)
        self.assertEqual(result[0][0].initial_settings["trigger"].source, "CH2")
        result[0][0].disconnect()

    def test_frame_delay_is_after_acquisition_and_queue_has_one_frame(self):
        class Adapter:
            channel_count = 1
            fetch_times = []
            commands = []
            def read_channel(self, _): return ChannelSettings()
            def read_timebase(self): return TimebaseSettings()
            def read_trigger(self): return TriggerSettings()
            def update_enabled_cache(self, _): pass
            def is_running(self): return True
            def fetch_all(self):
                self.fetch_times.append(time.monotonic())
                time.sleep(.04)
                return {"CH1": Waveform("CH1", np.arange(100.0), x_incr=1e-5)}
            def force_trigger(self): self.commands.append("force")
        adapter = Adapter()
        worker = AcquisitionWorker(adapter, interval_ms=150, settings_interval_ms=999999, wait_for_display=True)
        frames = []
        worker.dataReady.connect(frames.append)
        worker.start()
        try:
            self.assertTrue(pump_until(lambda: len(frames) == 1))
            pump_until(lambda: False, .25)
            self.assertEqual(len(adapter.fetch_times), 1, "未消费第一帧前禁止积压第二帧")
            worker.frame_consumed()
            self.assertTrue(pump_until(lambda: len(frames) == 2))
            self.assertGreater(adapter.fetch_times[1] - adapter.fetch_times[0], .4)
            worker.frame_consumed()
            worker.set_interval(1500)
            worker.submit("force_trigger")
            self.assertTrue(pump_until(lambda: bool(adapter.commands), .3), "控制命令不能等待刷新延迟")
            self.assertEqual(len(frames), 2)
        finally:
            self.assertTrue(worker.stop())

    def test_timeout_then_retry_keeps_buttons_and_close_working(self):
        window = MainWindow()
        errors = []
        try:
            with patch("scopeapp.ui.main_window.QMessageBox.warning", side_effect=lambda *args: errors.append(args[-1])):
                window._do_connect({"method": 1, "host": "127.0.0.1", "port": 1, "adapter": None})
                self.assertTrue(pump_until(lambda: not window.connector.isRunning()))
            self.assertTrue(errors)
            self.assertFalse(window.session.connected)
            self.assertTrue(window.btn_connect.isEnabled())
            window._start_simulator_and_connect()
            self.assertTrue(pump_until(lambda: window._frames > 0, 5))
            window.spn_refresh.setValue(2000)
            self.assertAlmostEqual(window.worker._interval, 2)
            self.assertTrue(window.session.adapter.full_record)
            self.assertIn("完整", window.lbl_record_mode.text())
            self.assertTrue(window.grab().save("artifacts/mso44_refresh_v0.6.png"))
        finally:
            window.close()


if __name__ == "__main__":
    mp.freeze_support()
    unittest.main(verbosity=2)
