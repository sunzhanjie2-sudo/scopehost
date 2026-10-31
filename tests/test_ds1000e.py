"""DS1152E 的真实 USB 采样回归，及二进制传输完整性检查。"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from scopeapp.adapters.rigol_ds1000e import RigolDs1000eAdapter
from scopeapp.archive import save_archive, load_archive
from scopeapp.measurements import measure
from scopeapp.model import ScopeIdentity, ChannelSettings, TimebaseSettings, TriggerSettings
from scopeapp.scpi import parse_ieee_block, ScpiError
from scopeapp.transport import VisaTransport, TcpTransport, TransportError

ROOT = Path(__file__).resolve().parents[1]

class DeviceFixture:
    identity = ScopeIdentity("Rigol Technologies", "DS1152E", "DS1EI182307208")
    def ask(self, command):
        replies = {":WAV:POIN:MODE?": "NORMAL", ":TIM:SCAL?": "0.0002",
                   ":TIM:OFFS?": "0", ":ACQ:SAMP?": "1250000",
                   ":CHAN1:COUP?": "DC", ":CHAN1:DISP?": "1",
                   ":CHAN1:SCAL?": "1", ":CHAN1:OFFS?": "-1.5"}
        replies[":CHAN1:BWL?"] = "OFF"
        return replies[command]
    def ask_binary(self, command):
        codes = np.load(ROOT / "artifacts/ds1152e_ch1_codes_200us.npy")
        return b"#800000600" + codes.tobytes()

class Ds1000eTests(unittest.TestCase):
    def test_real_calibration_data_matches_instrument_measurements(self):
        adapter = RigolDs1000eAdapter(DeviceFixture())
        wf = adapter.fetch_waveform(1)
        values = {m.name: m.value for m in measure(wf)}
        self.assertAlmostEqual(values["Vpp"], 3.0, delta=0.05)
        self.assertAlmostEqual(values["Freq"], 1000, delta=2)
        self.assertAlmostEqual(values["Vmin"], 0, delta=0.05)
        self.assertAlmostEqual(values["Vmax"], 3, delta=0.05)
        self.assertEqual(wf.points, 600)
        self.assertAlmostEqual(wf.duration, 0.0024)
        self.assertEqual(adapter.read_channel(1).screen_center, 1.5)
        with self.assertRaises(ValueError): adapter.read_channel(3)

    def test_settings_failure_is_not_replaced_with_invented_defaults(self):
        engine = DeviceFixture()
        engine.ask = lambda command: (_ for _ in ()).throw(TransportError("断开"))
        with self.assertRaises(TransportError): RigolDs1000eAdapter(engine).read_timebase()

    def test_floating_ch2_quantization_noise_has_no_period_measurement(self):
        from scopeapp.model import Waveform
        codes = np.load(ROOT / "artifacts/ds1152e_ch2_codes_200us.npy")
        wf = Waveform("CH2", (125-codes.astype(float))*.1/25+.1, x_incr=4e-6, raw=codes)
        values = {m.name:m.value for m in measure(wf)}
        self.assertLess(values["Vpp"], .02)
        self.assertEqual(values["Freq"], 0)

    def test_last_newline_codes_are_preserved(self):
        class Instrument:
            def read_raw(self): return b"#13" + bytes([125, 10, 13])
        transport = VisaTransport("USB-test")
        transport._inst = Instrument()
        payload, offset = parse_ieee_block(transport.read_raw())
        self.assertEqual(payload, bytes([125, 10, 13]))
        self.assertEqual(offset, 3)
        with self.assertRaises(ScpiError): parse_ieee_block(b"#14abc")

    def test_fragmented_tcp_block_does_not_stop_at_binary_newline(self):
        class Socket:
            chunks = iter([b"#15a\n", b"b\r\n"])
            def recv(self, size): return next(self.chunks)
        transport = TcpTransport("unused")
        transport._sock = Socket()
        payload, _ = parse_ieee_block(transport.read_raw())
        self.assertEqual(payload, b"a\nb\r\n")

    def test_archive_keeps_instrument_codes_and_grid_conventions(self):
        adapter = RigolDs1000eAdapter(DeviceFixture())
        wf = adapter.fetch_waveform(1)
        settings = {"channels": {"CH1": adapter.read_channel(1)},
                    "timebase": adapter.read_timebase(), "trigger": TriggerSettings()}
        with tempfile.TemporaryDirectory() as directory:
            path = save_archive(Path(directory)/"real.npz", {"CH1": wf}, settings, "DS1152E USB")
            waves, restored, meta = load_archive(path)
            np.testing.assert_array_equal(waves["CH1"].raw, wf.raw)
            self.assertEqual(restored["channels"]["CH1"].screen_center, 1.5)
            self.assertEqual(restored["timebase"].horizontal_divisions, 12)

if __name__ == "__main__": unittest.main(verbosity=2)
