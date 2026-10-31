"""DLM3000 manual-derived protocol fixtures; no claim of hardware validation."""
from __future__ import annotations

import os
from pathlib import Path
import socketserver
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage
from scopeapp.adapters import YokogawaDlm3000Adapter, detect_adapter, list_adapters
from scopeapp.archive import load_archive, save_archive
from scopeapp.model import ScopeIdentity
from scopeapp.scpi import ScpiEngine
from scopeapp.session import ScopeSession
from scopeapp.transport import TcpTransport, Transport
from scopeapp.ui.main_window import ConnectDialog, MainWindow
from scopeapp.ui.worker import AcquisitionWorker

APP = QApplication.instance() or QApplication([])
_image = QImage(24, 18, QImage.Format_RGB32)
_image.fill(0x182938)
_buffer = QBuffer()
_buffer.open(QIODevice.WriteOnly)
_image.save(_buffer, "PNG")
PNG = bytes(_buffer.data())


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate(): return True
        time.sleep(.01)
    return False


def block(payload):
    return b"#9" + f"{len(payload):09d}".encode() + payload


class DlmFixture(Transport):
    def __init__(self, model="DLM3054", points=8):
        self.model = model
        self.commands = []
        self.pending = None
        self.running = True
        self.channels = int(model[-1])
        self.selected = 1
        self.points = points
        self.start, self.end = 0, points - 1
        self.ocancel = 0
        self.sign = 1
        self.logic = 0
        self.trig_kind = "SIMPLE"
        self.all_payload = None
        self.image = PNG
        self.values = {
            ":TIMebase:TDIV": "0.0001", ":TIMebase:SRATe": "1000000",
            ":TRIGger:POSition": "25", ":TRIGger:MODE": "NORMAL",
            ":TRIGger:ATRigger:SIMPle:SOURce": "1",
            ":TRIGger:ATRigger:SIMPle:SLOPe": "RISE",
            ":TRIGger:ATRigger:SIMPle:LEVel": "0.5", ":IMAGe:FORMat": "JPEG",
            ":TRIGger:COMBination": "OFF",
        }
        for n in range(1, self.channels + 1):
            self.values.update({f":CHANnel{n}:VDIV": "0.5", f":CHANnel{n}:OFFSet": "1.25",
                               f":CHANnel{n}:POSition": "1", f":CHANnel{n}:DISPlay": "1",
                               f":CHANnel{n}:PROBe:MODE": "10",
                               f":CHANnel{n}:COUPling": "DC50", f":CHANnel{n}:BWIDth": "20000000"})

    def connect(self): pass
    def disconnect(self): pass

    def raw(self, channel):
        # Independent expected voltages: -0.75, 0.25, 1.25, 2.25, 3.25...
        return np.resize(np.array([-6400, -3200, 0, 3200, 6400], dtype="<i2"), self.points)

    def live_payload(self, trace_ids=None):
        traces = trace_ids or list(range(1, self.channels + 1))
        payload = struct.pack("<HQ", len(traces), 987654321)
        for n in traces:
            payload += struct.pack("<I8xI", n, self.points) + self.raw(n).tobytes()
        return payload

    def write(self, command):
        self.commands.append(command)
        for part in command.split(";"):
            value = self.respond(part)
            if value is not None:
                if self.pending is not None:
                    raise AssertionError("Unread response (e.g. SSTART? must be consumed)")
                self.pending = value if isinstance(value, bytes) else str(value).encode() + b"\n"

    def respond(self, command):
        key, _, value = command.partition(" ")
        if key == "*IDN?": return f"YOKOGAWA,{self.model},TEST,F1.00"
        if key in (":COMMunicate:HEADer", ":COMMunicate:REMote"): return None
        if key == ":STARt": self.running = True; return None
        if key == ":STOP": self.running = False; return None
        if key == ":STATus:CONDition?": return 5 if self.running else 0
        if key == ":SSTart?": self.running = True; self.values[":TRIGger:MODE"] = "NSINGLE"; return 0
        if key == ":TRIGger:FORCe": self.running = False; return None
        if key == ":ASETup:EXECute": return None
        if key == ":LOGic:MODE?": return self.logic
        if key == ":SYSTem:OCANcel?": return self.ocancel
        if key == ":TRIGger:ATRigger:TYPE?": return self.trig_kind
        if key == ":TRIGger:ATRigger:TYPE": self.trig_kind = value.upper(); return None
        if key == ":ACQuire:RLENgth?": return self.points
        if key == ":ACQuire:RLENgth": self.points = int(value); return None
        if key == ":WAVeform:TRACe": self.selected = int(value); return None
        if key in (":WAVeform:RECord", ":WAVeform:FORMat", ":WAVeform:BYTeorder", ":WAVeform:ALL:TRACe"): return None
        if key == ":WAVeform:LENGth?": return self.points
        if key == ":WAVeform:SRATe?": return 1000000
        if key == ":WAVeform:TRIGger?": return self.points / 2 + .25
        if key == ":WAVeform:RANGe?": return 1.0
        if key == ":WAVeform:OFFSet?": return 0 if self.ocancel else 1.25
        if key == ":WAVeform:SIGN?": return self.sign
        if key == ":WAVeform:STARt": self.start = int(value); return None
        if key == ":WAVeform:END": self.end = int(value); return None
        if key == ":WAVeform:SEND?": return block(self.raw(self.selected)[self.start:self.end+1].tobytes())
        if key == ":WAVeform:ALL:SEND?": return block(self.all_payload if self.all_payload is not None else self.live_payload())
        if key == ":IMAGe:SEND?": return block(self.image)
        if key.endswith("?") and key[:-1] in self.values: return self.values[key[:-1]]
        if key in self.values:
            self.values[key] = ("1" if value == "ON" else "0") if key.endswith(":DISPlay") else value.upper()
            return None
        raise AssertionError(f"Unspecified DLM3000 command: {command}")

    def read_raw(self):
        if self.pending is None: raise AssertionError("No fixture response")
        value, self.pending = self.pending, None
        return value


def adapter(model="DLM3054", points=8):
    device = DlmFixture(model, points)
    engine = ScpiEngine(device)
    engine.connect()
    return YokogawaDlm3000Adapter(engine), device


class DlmTests(unittest.TestCase):
    def test_identification_two_and_four_channels_only(self):
        for model in ("DLM3022", "DLM3032", "DLM3052", "DLM3024", "DLM3034", "DLM3054"):
            a, d = adapter(model)
            self.assertIs(detect_adapter(f"YOKOGAWA,{model},SN,F1"), YokogawaDlm3000Adapter)
            self.assertIs(detect_adapter(ScopeIdentity("YOKOGAWA", model).full), YokogawaDlm3000Adapter)
            self.assertEqual(a.channel_count, int(model[-1]))
            self.assertEqual(len(a.fetch_all()), int(model[-1]))
        for model in ("DLM3054HD", "DLM4038", "DL850"):
            self.assertIsNone(detect_adapter(f"YOKOGAWA,{model},SN,F1"))
        self.assertIn(YokogawaDlm3000Adapter.name, list_adapters())

    def test_running_register_checks_run_bit_not_trigger_or_cursor_bits(self):
        a, d = adapter()
        self.assertTrue(a.is_running())
        d.running = False
        original = d.respond
        d.respond = lambda cmd: 4 if cmd == ":STATus:CONDition?" else original(cmd)
        self.assertFalse(a.is_running())

    def test_nonblocking_single_force_and_return_to_run(self):
        a, d = adapter()
        a.single()
        self.assertIn(":SSTart? 0", d.commands)
        self.assertIsNone(d.pending)
        self.assertTrue(a.is_running())
        a.force_trigger()
        self.assertFalse(a.is_running())
        self.assertEqual(a.read_trigger().mode, "SINGLE")
        self.assertEqual(len(a.fetch_all()), 4)
        a.run()
        self.assertTrue(a.is_running())
        self.assertEqual(d.values[":TRIGger:MODE"], "AUTO")

    def test_basic_controls_read_back_real_dlm_names(self):
        a, d = adapter()
        a.stop(); a.set_timebase(.002); a.set_channel_scale(1, .2)
        a.set_channel_offset(1, -.4); a.set_channel_coupling(1, "AC")
        a.set_trigger_source("CH2"); a.set_trigger_level(.125); a.set_trigger_slope("FALL")
        a.set_trigger_mode("NORMAL"); a.autoset()
        ch, tb, tr = a.read_channel(1), a.read_timebase(), a.read_trigger()
        self.assertEqual((ch.scale, ch.offset, ch.coupling.value), (.2, -.4, "AC"))
        self.assertAlmostEqual(ch.screen_center, -.6)
        self.assertEqual(tb.scale, .002)
        self.assertAlmostEqual(tb.position, .005)
        self.assertEqual((tr.source, tr.level, tr.slope, tr.mode), ("CH2", .125, "FALL", "NORMAL"))

    def test_bandwidth_full_and_real_cutoffs(self):
        a, d = adapter()
        for value, label in (("FULL", "FULL"), ("20000000", "20 MHz"), ("8000", "8 kHz")):
            d.values[":CHANnel1:BWIDth"] = value
            self.assertEqual(a.read_channel(1).bandwidth, label)
        d.values[":CHANnel1:BWIDth"] = "nan"
        with self.assertRaises(ValueError): a.read_channel(1)

    def test_no_nonexistent_channels_and_no_ground_coupling(self):
        a, d = adapter("DLM3052")
        before = len(d.commands)
        with self.assertRaises(ValueError): a.set_channel_scale(3, 1)
        with self.assertRaises(ValueError): a.set_trigger_source("CH4")
        with self.assertRaises(ValueError): a.set_channel_coupling(1, "GND")
        self.assertEqual(len(d.commands), before)

    def test_live_channels_share_acquisition_and_physical_axes(self):
        a, d = adapter()
        wfs = a.fetch_all()
        for wf in wfs.values():
            np.testing.assert_allclose(wf.y[:5], [-.75, .25, 1.25, 2.25, 3.25])
            self.assertEqual(wf.x_start, -4.25e-6)
            self.assertEqual(wf.x_incr, 1e-6)
            self.assertIn("987654321", wf.decode_note)
        self.assertEqual(d.commands.count(":WAVeform:ALL:SEND? 0"), 1)
        self.assertNotIn(":STOP", d.commands)

    def test_disabled_channels_ignored_but_missing_enabled_channel_rejected(self):
        a, d = adapter()
        self.assertTrue(a.set_channel_enabled(2, False))
        self.assertNotIn("CH2", a.fetch_all())
        d.all_payload = d.live_payload([1, 2, 3])
        with self.assertRaisesRegex(ValueError, "缺少"): a.fetch_all()

    def test_first_fetch_observes_disabled_channels_without_worker_cache(self):
        d = DlmFixture("DLM3052")
        d.values[":CHANnel2:DISPlay"] = "0"
        engine = ScpiEngine(d)
        engine.connect()
        a = YokogawaDlm3000Adapter(engine)
        self.assertEqual(set(a.fetch_all()), {"CH1"})
        self.assertNotIn(":WAVeform:TRACe 2;:WAVeform:RECord 0", d.commands)

    def test_extra_math_trace_does_not_become_a_fake_analog_channel(self):
        a, d = adapter()
        d.all_payload = d.live_payload([1, 2, 3, 4, 9])
        self.assertEqual(set(a.fetch_all()), {"CH1", "CH2", "CH3", "CH4"})

    def test_logic_mode_does_not_show_ch4_as_analog(self):
        a, d = adapter()
        d.logic = 1
        settings = {f"CH{n}": a.read_channel(n) for n in range(1, 5)}
        a.update_enabled_cache(settings)
        self.assertFalse(settings["CH4"].enabled)
        self.assertNotIn("CH4", a.fetch_all())
        with self.assertRaisesRegex(RuntimeError, "逻辑"): a.set_channel_enabled(4, True)

    def test_stopped_long_record_is_chunked_without_changing_acquisition(self):
        a, d = adapter(points=200_005)
        d.running = False
        wf = a.fetch_waveform(1)
        self.assertEqual(wf.points, 200_005)
        self.assertEqual(d.commands.count(":WAVeform:SEND?"), 3)
        np.testing.assert_array_equal(wf.raw, d.raw(1))
        self.assertEqual(wf.x_start, -100002.75e-6)
        self.assertNotIn(":STARt", d.commands)
        self.assertNotIn(":WAVeform:ALL:SEND? 0", d.commands)

    def test_offset_cancellation_still_returns_physical_input_voltage(self):
        a, d = adapter()
        d.ocancel = 1; d.running = False
        np.testing.assert_allclose(a.fetch_waveform(1).y[:5], [-.75, .25, 1.25, 2.25, 3.25])

    def test_malformed_live_blocks_are_rejected(self):
        a, d = adapter()
        good = d.live_payload()
        wrong_points = bytearray(good)
        struct.pack_into("<I", wrong_points, 22, 7)
        for bad in (b"", good[:-1], good+b"x", bytes(wrong_points), d.live_payload([1, 1, 3, 4])):
            d.all_payload = bad
            with self.assertRaises(ValueError): a.fetch_all()

    def test_invalid_metadata_and_transfer_limits_do_not_fabricate_data(self):
        a, d = adapter()
        d.values[":TRIGger:MODE"] = "UNKNOWN"
        with self.assertRaises(ValueError): a.read_trigger()
        d.values[":TRIGger:MODE"] = "NORMAL"
        original = d.respond
        d.respond = lambda cmd: "nan" if cmd == ":WAVeform:SRATe?" else original(cmd)
        with self.assertRaises(ValueError): a.fetch_all()
        d.respond = original
        d.points = 12501
        with self.assertRaisesRegex(RuntimeError, "12.5k"): a.fetch_all()
        d.points = 2_000_001; d.running = False
        with self.assertRaisesRegex(RuntimeError, "200 万"): a.fetch_waveform(1)

    def test_enhanced_trigger_is_not_reported_as_edge(self):
        a, d = adapter()
        d.trig_kind = "CAN"
        with self.assertRaisesRegex(NotImplementedError, "增强"): a.read_trigger()
        a.set_trigger_source("CH1")
        self.assertEqual(a.read_trigger().source, "CH1")

    def test_current_probe_and_combination_trigger_are_not_mislabeled(self):
        a, d = adapter()
        d.values[":CHANnel1:PROBe:MODE"] = "C0_1"
        with self.assertRaisesRegex(NotImplementedError, "电流"): a.fetch_all()
        with self.assertRaisesRegex(NotImplementedError, "电流"): a.read_channel(1)
        d.values[":TRIGger:COMBination"] = "ABN"
        with self.assertRaisesRegex(NotImplementedError, "增强"): a.read_trigger()
        a.set_trigger_source("CH2")
        self.assertEqual(a.read_trigger().source, "CH2")

    def test_record_length_control_and_readback(self):
        a, d = adapter()
        a.set_record_length(12500)
        self.assertEqual(a.read_timebase().record_length, 12500)
        self.assertEqual(a.fetch_all()["CH1"].points, 12500)
        with self.assertRaises(ValueError): a.set_record_length(2_000_001)

    def test_native_screen_capture_and_format_restoration_on_failure(self):
        a, d = adapter()
        self.assertEqual(a.capture_screen(), PNG)
        self.assertEqual(QImage.fromData(PNG).size(), _image.size())
        self.assertEqual(d.values[":IMAGe:FORMat"], "JPEG")
        self.assertNotIn(":IMAGe:EXECute", d.commands)
        d.image = b"not a PNG"
        with self.assertRaises(ValueError): a.capture_screen()
        self.assertEqual(d.values[":IMAGe:FORMat"], "JPEG")

    def test_archive_preserves_dc50_position_raw_data_and_bandwidth(self):
        a, d = adapter("DLM3052")
        wfs = a.fetch_all()
        settings = dict(channels={f"CH{n}": a.read_channel(n) for n in (1, 2)},
                        timebase=a.read_timebase(), trigger=a.read_trigger())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dlm.npz"
            save_archive(path, wfs, settings)
            restored, meta, _ = load_archive(path)
        self.assertEqual(meta["channels"]["CH1"].coupling.value, "DC50")
        self.assertEqual(meta["channels"]["CH1"].screen_center, .75)
        self.assertEqual(meta["channels"]["CH1"].bandwidth, "20 MHz")
        np.testing.assert_array_equal(restored["CH1"].raw, wfs["CH1"].raw)

    def test_gui_adapter_port_preserves_custom_port_and_archive_coupling(self):
        with patch("scopeapp.ui.main_window.VisaProcessTransport.scan", return_value=[]):
            dialog = ConnectDialog()
            dialog.adapter.setCurrentText(YokogawaDlm3000Adapter.name)
            self.assertEqual(dialog.port.value(), 10002)
            dialog.port.setValue(1234)
            dialog.adapter.setCurrentIndex(0)
            self.assertEqual(dialog.port.value(), 1234)
            dialog.close()
        a, d = adapter("DLM3052")
        window = MainWindow()
        window._configure_channels(["CH1", "CH2"])
        settings = dict(channels={f"CH{n}": a.read_channel(n) for n in (1, 2)},
                        timebase=a.read_timebase(), trigger=a.read_trigger())
        window._on_settings(settings)
        self.assertEqual(window._ch_widgets[1]["coupling"].currentText(), "DC50")
        self.assertTrue(window._ch_widgets[3]["group"].isHidden())
        window.close()

    def test_worker_waits_for_single_and_freezes_matching_channels(self):
        a, d = adapter("DLM3052")
        worker = AcquisitionWorker(a, interval_ms=20, settings_interval_ms=50)
        frames, errors, states = [], [], []
        worker.dataReady.connect(frames.append)
        worker.error.connect(errors.append)
        worker.stateChanged.connect(states.append)
        worker.submit("single")
        worker.start()
        try:
            self.assertTrue(wait_for(lambda: "ARMED" in states), (errors, states))
            self.assertEqual(frames, [])
            worker.submit("force_trigger")
            self.assertTrue(wait_for(lambda: "SINGLE DONE" in states), (errors, states))
            self.assertEqual(set(frames[-1]["waveforms"]), {"CH1", "CH2"})
            self.assertIn("SINGLE DONE", states)
            count = len(frames)
            QTest.qWait(120)
            self.assertEqual(len(frames), count)
            self.assertEqual(errors, [])
        finally:
            self.assertTrue(worker.stop())

    def test_connected_gui_limits_options_and_sets_record_length(self):
        fixture = DlmFixture("DLM3052", points=1250)
        session = ScopeSession()
        session._connect(fixture, None)
        window = MainWindow()
        errors, slot_errors = [], []
        previous_hook = sys.excepthook
        sys.excepthook = lambda kind, value, trace: slot_errors.append(str(value))
        window.show()
        try:
            window._connected((session, None))
            window._source = "DLM3000 协议测试 · SIMULATION"
            window._show_source(window._source)
            window.worker.error.connect(errors.append)
            self.assertTrue(wait_for(lambda: window._frames > 0), errors)
            self.assertTrue(window.cmb_record_length.isVisible())
            self.assertEqual([window._ch_widgets[1]["coupling"].itemText(i) for i in range(3)], ["DC", "AC", "DC50"])
            self.assertGreaterEqual(window.cmb_trig_slope.findData("BOTH"), 0)
            self.assertGreaterEqual(window.cmb_trig_mode.findData("ALEVEL"), 0)
            window.cmb_record_length.setCurrentIndex(window.cmb_record_length.findData(12500))
            self.assertTrue(wait_for(lambda: window._settings["timebase"].record_length == 12500), errors)
            self.assertTrue(wait_for(lambda: window._raw_waveforms["CH1"].points == 12500), errors)
            self.assertEqual(set(window._raw_waveforms), {"CH1", "CH2"})
            self.assertTrue(window._ch_widgets[3]["group"].isHidden())
            self.assertEqual(errors, [])
            self.assertEqual(slot_errors, [])
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "native.png"
                with patch.object(window, "_path", return_value=str(path)):
                    window._on_screenshot()
                self.assertTrue(wait_for(path.exists), errors)
                self.assertEqual(QImage(str(path)).size(), _image.size())
                self.assertEqual(fixture.values[":IMAGe:FORMat"], "JPEG")
            window.grab().save(str(ROOT / "artifacts/dlm3000_protocol_ui.png"))
        finally:
            window.close()
            sys.excepthook = previous_hook

    def test_tcp_late_block_terminator_does_not_desynchronize_next_query(self):
        class Socket:
            def __init__(self): self.chunks = iter([block(b"\n\rword"), b"\r", b"\n", b"42\n"])
            def recv(self, size): return next(self.chunks)
        tcp = TcpTransport("fixture")
        tcp._sock = Socket()
        self.assertEqual(tcp.read_raw(), block(b"\n\rword"))
        self.assertEqual(tcp.read_raw(), b"42")

    def test_socket_session_identification_binary_read_and_screenshot(self):
        fixture = DlmFixture("DLM3052")
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    fixture.write(line.decode().strip())
                    if fixture.pending is not None:
                        reply = fixture.read_raw()
                        self.wfile.write(reply); self.wfile.flush()
                        if reply.startswith(b"#"):
                            time.sleep(.005)
                            self.wfile.write(b"\n"); self.wfile.flush()
        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = ScopeSession()
        try:
            a = session.connect_tcp("127.0.0.1", server.server_address[1])
            self.assertIsInstance(a, YokogawaDlm3000Adapter)
            self.assertEqual(len(a.fetch_all()), 2)
            self.assertEqual(a.capture_screen(), PNG)
            self.assertEqual(a.read_channel(1).coupling.value, "DC50")
        finally:
            session.disconnect(); server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    unittest.main(verbosity=2)
