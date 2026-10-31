"""完整 Tek 深存储记录的协议回归；夹具结果不代替实机验收。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scopeapp.adapters.tektronix import TektronixAdapter
from scopeapp.scpi import ScpiEngine
from scopeapp.transport import Transport


class FullRecordFixture(Transport):
    """记录每段真实索引，冻结前会产生不同代数据，拒绝默默跳点。"""

    def __init__(self, record=1001, running=False, stop_after="RUNSTOP"):
        self.record = record
        self.running = running
        self.stop_after = stop_after
        self.source = 1
        self.start = 1
        self.stop = record
        self.stride = 7  # 验证此前仪器的抽取设置会被显式清除。
        self.header = False
        self.commands = []
        self.ranges = []
        self.curve_count = 0
        self.generation = 0
        self.result = None
        self.failure = None
        self.available_sources = "CH1,CH2,CH3,CH4"

    def connect(self):
        pass

    def disconnect(self):
        pass

    @staticmethod
    def raw_samples(channel, indices):
        # 包含两个相邻点的窄脉冲，抽取/插值会直接破坏它。
        pulse = np.isin(indices % 101, [50, 51])
        return (np.where(pulse, 300, -25) + channel * 11).astype(">i2")

    def write(self, command):
        self.commands.append(command)
        for part in command.upper().split(";"):
            head, _, arg = part.lstrip(":").partition(" ")
            if head == "*IDN?":
                self.result = "TEKTRONIX,MSO44,FIXTURE,FW"
            elif head == "HEADER":
                self.header = arg in ("ON", "1")
            elif head == "VERBOSE":
                pass
            elif head == "ACQUIRE:STATE?":
                self.result = str(int(self.running))
            elif head == "ACQUIRE:NUMACQ?":
                self.result = str(self.generation)
            elif head == "ACQUIRE:STATE":
                self.running = arg == "RUN"
            elif head == "ACQUIRE:STOPAFTER?":
                self.result = self.stop_after
            elif head == "*OPC?":
                self.result = "1"
            elif head == "HORIZONTAL:RECORDLENGTH?":
                self.result = str(self.record)
            elif head == "DATA:SOURCE":
                self.source = int(arg[2:])
            elif head == "DATA:SOURCE:AVAILABLE?":
                self.result = self.available_sources
            elif head == "DATA:RESAMPLE":
                self.stride = int(arg)
            elif head == "DATA:START":
                self.start = int(arg)
            elif head == "DATA:STOP":
                self.stop = int(arg)
            elif head.startswith("WFMOUTPRE:") and arg:
                pass
            elif head == "WFMOUTPRE?":
                count = self.stop - self.start + 1
                pt_off = self.record / 2 - (self.start - 1)
                ymult, xzero = .005, 1e-10
                if self.curve_count and self.failure == "axis":
                    pt_off += 1
                elif self.curve_count and self.failure == "scale":
                    ymult *= 2
                elif self.curve_count and self.failure == "xzero":
                    xzero += 1e-9
                if self.failure == "preamble_count":
                    count -= 1
                self.result = ";".join([
                    "BYT_NR 2", "BIT_NR 16", "ENCDG BINARY", "BN_FMT RI", "BYT_OR MSB",
                    f'WFID "CH{self.source}"', f"NR_PT {count}", "PT_FMT Y", "PT_ORDER LINEAR",
                    'XUNIT "s"', "XINCR 2e-9", f"XZERO {xzero}", f"PT_OFF {pt_off}",
                    'YUNIT "V"', f"YMULT {ymult}", "YOFF .5", "YZERO .2",
                ])
                if self.failure == "units":
                    self.result = self.result.replace('XUNIT "s"', 'XUNIT "Hz"')
                elif self.failure == "byte_count":
                    self.result = self.result.replace("BYT_NR 2", "BYT_NR 2.5")
            elif head == "WAVFRM?":
                self.write(":WFMOUTPRE?")
                preamble = self.result.encode()
                header = self.header
                self.header = False
                self.write(":CURVE?")
                self.header = header
                self.result = preamble + b";:CURVE " + self.result
            elif head == "CURVE?":
                if self.header:
                    raise AssertionError("二进制响应时头必须关闭")
                if self.running:
                    self.generation += 1
                self.ranges.append((self.source, self.start, self.stop))
                indices = np.arange(self.start - 1, self.stop, self.stride)
                data = (self.raw_samples(self.source, indices) + self.generation).astype(">i2").tobytes()
                if self.failure == "data_count":
                    data = data[:-2]
                size = str(len(data)).encode()
                self.result = b"#" + str(len(size)).encode() + size + data
                self.curve_count += 1
                if self.failure == "truncated":
                    self.result = self.result[:-1]
                elif self.failure == "running":
                    self.running = True
                elif self.failure == "depth":
                    self.record += 1
            else:
                raise AssertionError(f"完整记录夹具拒绝未知命令：{part}")

    def read_raw(self):
        if self.result is None:
            raise AssertionError("没有响应")
        return self.result if isinstance(self.result, bytes) else (self.result + "\n").encode()


def fixture_adapter(**kwargs):
    fixture = FullRecordFixture(**kwargs)
    engine = ScpiEngine(fixture)
    engine.connect()
    adapter = TektronixAdapter(engine)
    adapter.transfer_chunk_points = 333
    adapter._enabled_cache = {1: True, 2: True, 3: False, 4: False}
    return adapter, fixture


class FullRecordTests(unittest.TestCase):
    def test_default_reads_every_real_sample_across_partial_last_block(self):
        adapter, fixture = fixture_adapter()
        wf = adapter.fetch_waveform(1)
        expected = fixture.raw_samples(1, np.arange(fixture.record))
        self.assertTrue(adapter.full_record)
        self.assertEqual(wf.points, fixture.record)
        np.testing.assert_array_equal(wf.raw, expected)
        np.testing.assert_array_equal(wf.y, (expected.astype(float) - .5) * .005 + .2)
        self.assertAlmostEqual(wf.x_start, -fixture.record / 2 * 2e-9 + 1e-10)
        self.assertEqual(wf.x_incr, 2e-9)
        self.assertEqual(wf.device_record_length, fixture.record)
        self.assertEqual(wf.sample_stride, 1)
        self.assertTrue(wf.full_record)
        self.assertEqual(fixture.stride, 1)
        self.assertEqual(fixture.ranges, [(1, 1, 333), (1, 334, 666), (1, 667, 999), (1, 1000, 1001)])
        self.assertFalse(fixture.running)
        self.assertEqual(fixture.stop_after, "RUNSTOP")

    def test_all_channels_share_the_user_stopped_acquisition(self):
        adapter, fixture = fixture_adapter()
        waveforms = adapter.fetch_all()
        self.assertEqual(set(waveforms), {"CH1", "CH2"})
        for channel, wf in waveforms.items():
            np.testing.assert_array_equal(wf.raw, fixture.raw_samples(int(channel[-1]), np.arange(fixture.record)))
        self.assertEqual(fixture.generation, 0)
        self.assertEqual(fixture.commands.count(":ACQuire:STATE STOP"), 0)
        self.assertEqual(fixture.commands.count(":ACQuire:STATE RUN"), 0)
        self.assertEqual(waveforms["CH1"].x_start, waveforms["CH2"].x_start)

    def test_all_channel_precheck_rejects_missing_record_before_any_curve(self):
        adapter, fixture = fixture_adapter(running=False)
        fixture.available_sources = "CH1"
        with self.assertRaisesRegex(RuntimeError, "CH2.*RUN.*Single"):
            adapter.fetch_all()
        self.assertEqual(fixture.curve_count, 0)
        self.assertEqual(fixture.ranges, [])
        self.assertFalse(fixture.running)

    def test_single_channel_precheck_requires_requested_analog_source(self):
        adapter, fixture = fixture_adapter()
        fixture.available_sources = "CH1,CH2_SV_NORMAL,MATH1,REF1"
        with self.assertRaisesRegex(RuntimeError, "CH2.*已采集记录"):
            adapter.fetch_waveform(2)
        self.assertEqual(fixture.curve_count, 0)
        self.assertFalse(fixture.running)

    def test_no_acquired_sources_never_send_curve(self):
        adapter, fixture = fixture_adapter()
        fixture.available_sources = "NONE"
        with self.assertRaisesRegex(RuntimeError, "CH1, CH2.*已采集记录"):
            adapter.fetch_all()
        self.assertEqual(fixture.curve_count, 0)
        self.assertFalse(fixture.running)

    def test_source_list_accepts_header_and_ignores_nonanalog_waveforms(self):
        adapter, fixture = fixture_adapter()
        fixture.available_sources = ":DATA:SOURCE:AVAILABLE CH1, CH2, MATH1, REF1, CH2_SV_NORMAL"
        waveforms = adapter.fetch_all()
        self.assertEqual(set(waveforms), {"CH1", "CH2"})
        self.assertEqual(fixture.commands.count(":DATa:SOUrce:AVAILable?"), 1)

    def test_malformed_source_list_is_rejected(self):
        for response in ("", "CH1;;CH2", "0"):
            with self.subTest(response=response):
                adapter, fixture = fixture_adapter()
                fixture.available_sources = response
                with self.assertRaisesRegex(ValueError, "源列表"):
                    adapter.fetch_all()
                self.assertEqual(fixture.curve_count, 0)
                self.assertFalse(fixture.running)

    def test_stopped_record_is_not_restarted(self):
        adapter, fixture = fixture_adapter(running=False)
        adapter.fetch_all()
        self.assertFalse(fixture.running)
        self.assertFalse(any(command.startswith(":ACQuire:STATE ") for command in fixture.commands))

    def test_completed_single_is_not_rearmed(self):
        adapter, fixture = fixture_adapter(running=False, stop_after="SEQUENCE")
        adapter.fetch_all()
        self.assertFalse(fixture.running)
        self.assertEqual(fixture.stop_after, "SEQUENCE")
        self.assertFalse(any(command.startswith(":ACQuire:STATE ") for command in fixture.commands))

    def test_pending_single_is_not_cancelled_for_readout(self):
        adapter, fixture = fixture_adapter(running=True, stop_after="SEQUENCE")
        with self.assertRaisesRegex(RuntimeError, "Single 尚未完成"):
            adapter.fetch_all()
        self.assertTrue(fixture.running)
        self.assertEqual(fixture.curve_count, 0)
        self.assertFalse(any(command.startswith(":ACQuire:STATE ") for command in fixture.commands))

    def test_progress_counts_real_received_points(self):
        adapter, fixture = fixture_adapter()
        progress = []
        adapter.waveform_progress = progress.append
        adapter.fetch_all()
        self.assertEqual(len(progress), 8)
        self.assertEqual(progress[-1], {"channel": "CH2", "channel_points": 1001, "record_points": 1001,
                                        "received_points": 2002, "total_points": 2002, "running": False})
        self.assertEqual([item["received_points"] for item in progress], [333, 666, 999, 1001, 1334, 1667, 2000, 2002])

    def test_frame_settings_callback_reads_once_in_user_stopped_record(self):
        adapter, fixture = fixture_adapter()
        observations = []

        def read_settings():
            observations.append((fixture.running, adapter._full_record_points, fixture.curve_count))

        adapter.frame_settings_callback = read_settings
        adapter.fetch_all()
        self.assertEqual(observations, [(False, fixture.record, 0)])
        self.assertFalse(fixture.running)

    def test_frame_settings_callback_channel_cache_controls_this_frame(self):
        adapter, fixture = fixture_adapter()
        progress = []
        adapter.waveform_progress = progress.append
        adapter.frame_settings_callback = lambda: adapter._enabled_cache.__setitem__(2, False)
        waveforms = adapter.fetch_all()
        self.assertEqual(set(waveforms), {"CH1"})
        self.assertEqual(progress[-1]["total_points"], fixture.record)
        self.assertFalse(any(channel == 2 for channel, _, _ in fixture.ranges))

    def test_changed_depth_during_frame_settings_rejects_before_waveform_read(self):
        adapter, fixture = fixture_adapter()
        adapter.frame_settings_callback = lambda: setattr(fixture, "record", fixture.record + 1)
        with self.assertRaisesRegex(ValueError, "设置读回期间变化"):
            adapter.fetch_all()
        self.assertEqual(fixture.curve_count, 0)
        self.assertFalse(fixture.running)

    def test_incomplete_binary_block_is_rejected_without_restarting(self):
        for failure in ("data_count", "truncated", "preamble_count"):
            with self.subTest(failure=failure):
                adapter, fixture = fixture_adapter()
                fixture.failure = failure
                with self.assertRaises(Exception):
                    adapter.fetch_all()
                self.assertFalse(fixture.running)
                self.assertFalse(fixture.header)

    def test_inconsistent_scaling_or_timing_is_rejected(self):
        for failure in ("axis", "scale", "xzero"):
            with self.subTest(failure=failure):
                adapter, fixture = fixture_adapter()
                fixture.failure = failure
                with self.assertRaisesRegex(ValueError, "不连续|改变"):
                    adapter.fetch_all()
                self.assertFalse(fixture.running)

    def test_unknown_units_and_fractional_byte_width_are_rejected(self):
        for failure in ("units", "byte_count"):
            with self.subTest(failure=failure):
                adapter, fixture = fixture_adapter()
                fixture.failure = failure
                with self.assertRaisesRegex(ValueError, "单位|编码"):
                    adapter.fetch_all()
                self.assertFalse(fixture.running)

    def test_changed_record_depth_or_acquisition_rejects_entire_frame(self):
        for failure in ("depth", "running"):
            with self.subTest(failure=failure):
                adapter, fixture = fixture_adapter()
                fixture.failure = failure
                with self.assertRaisesRegex(ValueError, "变化|重新运行|不连续"):
                    adapter.fetch_all()
                self.assertEqual(fixture.running, failure == "running")

    def test_run_reads_one_complete_packet_per_channel_without_state_commands(self):
        adapter, fixture = fixture_adapter(running=True)
        waveforms = adapter.fetch_all()
        self.assertTrue(fixture.running)
        self.assertEqual(fixture.ranges, [(1, 1, 1001), (2, 1, 1001)])
        self.assertEqual(fixture.commands.count(":WAVFrm?"), 2)
        self.assertFalse(any("ACQUIRE:STATE " in command.upper() for command in fixture.commands))
        for generation, name in enumerate(("CH1", "CH2"), 1):
            expected = fixture.raw_samples(int(name[-1]), np.arange(fixture.record)) + generation
            np.testing.assert_array_equal(waveforms[name].raw, expected)
            self.assertTrue(waveforms[name].full_record)
            self.assertFalse(waveforms[name].record_stable)

    def test_run_read_failure_does_not_stop_or_restart_the_instrument(self):
        adapter, fixture = fixture_adapter(running=True)
        fixture.failure = "data_count"
        with self.assertRaises(ValueError):
            adapter.fetch_all()
        self.assertTrue(fixture.running)
        self.assertFalse(fixture.header)
        self.assertFalse(any("ACQUIRE:STATE " in command.upper() for command in fixture.commands))

    def test_run_cancel_only_restores_response_header(self):
        adapter, fixture = fixture_adapter(running=True)
        cancelled = [False]
        def guard():
            if cancelled[0] and not adapter._restoring_protocol:
                raise RuntimeError("requested stop")
        adapter.engine.before_operation = guard
        adapter.waveform_progress = lambda p: cancelled.__setitem__(0, p["channel_points"] > 0)
        with self.assertRaisesRegex(RuntimeError, "requested stop"):
            adapter.fetch_all()
        self.assertTrue(fixture.running)
        self.assertEqual(fixture.curve_count, 1)
        self.assertFalse(any("ACQUIRE:STATE " in command.upper() for command in fixture.commands))

    def test_new_acquisition_even_if_stopped_again_invalidates_the_record(self):
        adapter, fixture = fixture_adapter()
        adapter.waveform_progress = lambda _: setattr(fixture, "generation", 1)
        with self.assertRaisesRegex(ValueError, "被新采集刷新"):
            adapter.fetch_all()
        self.assertFalse(fixture.running)

    def test_cooperative_cancel_allows_only_cleanup_and_never_returns_partial_frame(self):
        adapter, fixture = fixture_adapter()
        cancelled = [False]
        original_guard_calls = []

        def guard():
            original_guard_calls.append(True)
            if cancelled[0] and not adapter._restoring_protocol:
                raise RuntimeError("requested stop")

        adapter.engine.before_operation = guard
        adapter.waveform_progress = lambda _: cancelled.__setitem__(0, True)
        with self.assertRaisesRegex(RuntimeError, "requested stop"):
            adapter.fetch_all()
        self.assertFalse(fixture.running)
        self.assertEqual(fixture.curve_count, 1)
        self.assertFalse(fixture.header)
        self.assertTrue(original_guard_calls)

    def test_cancellation_during_preamble_restores_only_protocol_header(self):
        adapter, fixture = fixture_adapter()
        original_read = fixture.read_raw
        cancelled = [False]

        def read():
            response = original_read()
            if fixture.commands[-1] == ":WFMOutpre?":
                cancelled[0] = True
            return response

        def guard():
            if cancelled[0] and not adapter._restoring_protocol:
                raise RuntimeError("requested stop")

        fixture.read_raw = read
        adapter.engine.before_operation = guard
        with self.assertRaisesRegex(RuntimeError, "requested stop"):
            adapter.fetch_waveform(1)
        self.assertFalse(fixture.running)
        self.assertFalse(fixture.header)
        self.assertEqual(fixture.curve_count, 0)

    def test_no_enabled_channels_does_not_stop_or_query_instrument(self):
        adapter, fixture = fixture_adapter()
        adapter._enabled_cache = dict.fromkeys(range(1, 5), False)
        count = len(fixture.commands)
        self.assertEqual(adapter.fetch_all(), {})
        self.assertEqual(len(fixture.commands), count)

    def test_wavfrm_alternate_entry_never_falls_back_to_preview(self):
        adapter, fixture = fixture_adapter()
        self.assertEqual(adapter.fetch_wavfrm(1).points, fixture.record)
        self.assertEqual(fixture.stride, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
