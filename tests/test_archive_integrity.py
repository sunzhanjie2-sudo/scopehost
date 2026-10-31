"""NPZ 保留真实采样个数、抽点来源与完整性；无需仪器。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scopeapp.archive import (DEFAULT_MAX_UNCOMPRESSED_BYTES, load_archive,
                              save_archive)
from scopeapp.model import ChannelSettings, TimebaseSettings, TriggerSettings, Waveform


def capture(points=1000, depth=1000, rate=250_000_000, stride=1, enabled=(1,)):
    settings = {"channels": {f"CH{n}": ChannelSettings(number=n, enabled=n in enabled)
                              for n in range(1, 5)},
                "timebase": TimebaseSettings(record_length=depth, sample_rate=rate),
                "trigger": TriggerSettings()}
    waveforms = {}
    for number in enabled:
        raw = np.arange(points, dtype=np.int16)
        waveforms[f"CH{number}"] = Waveform(f"CH{number}", raw.astype(np.float64) * .005,
                                             x_start=-2e-6, x_incr=stride / rate,
                                             raw=raw, source_encoding="INT16")
    return waveforms, settings


def rewrite(path: Path, change):
    with np.load(path, allow_pickle=False) as source:
        arrays = {name: source[name] for name in source.files}
    metadata = json.loads(str(arrays["metadata"].item()))
    change(metadata, arrays)
    arrays["metadata"] = np.array(json.dumps(metadata, ensure_ascii=False))
    with path.open("wb") as output:
        np.savez_compressed(output, **arrays)


class ArchiveIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "capture.npz"

    def tearDown(self):
        self.directory.cleanup()

    def test_complete_capture_is_lossless_with_actual_counts(self):
        waves, settings = capture(enabled=(1, 2, 3, 4))
        save_archive(self.path, waves, settings, require_complete=True)
        recovered, _, metadata = load_archive(self.path)
        self.assertEqual(metadata["acquisition"]["completeness"], "complete")
        for name, original in waves.items():
            np.testing.assert_array_equal(recovered[name].raw, original.raw)
            np.testing.assert_array_equal(recovered[name].y, original.y)
            self.assertEqual(recovered[name].points, 1000)
            info = metadata["waveforms"][name]
            self.assertEqual(info["points"], 1000)
            self.assertEqual(info["device_record_length"], 1000)
            self.assertEqual(info["sample_stride"], 1)
            self.assertTrue(info["full_record"])
            self.assertEqual(info["completeness_basis"], "device_settings")

    def test_preview_keeps_actual_point_count_and_cannot_be_complete(self):
        waves, settings = capture(points=100, depth=1000, stride=10)
        save_archive(self.path, waves, settings)
        restored, _, meta = load_archive(self.path)
        self.assertEqual(restored["CH1"].points, 100)
        self.assertEqual(meta["waveforms"]["CH1"]["points"], 100)
        self.assertEqual(meta["waveforms"]["CH1"]["sample_stride"], 10)
        self.assertEqual(meta["acquisition"]["completeness"], "incomplete")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "100.*1000"):
            save_archive(self.path, waves, settings, require_complete=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_full_size_decimated_waveform_is_rejected_by_sample_interval(self):
        waves, settings = capture(stride=2)
        with self.assertRaisesRegex(ValueError, "采样间隔"):
            save_archive(self.path, waves, settings, require_complete=True)
        self.assertFalse(self.path.exists())

    def test_explicit_preview_provenance_rejects_upsampled_point_count(self):
        waves, settings = capture()
        waves["CH1"].full_record = False
        waves["CH1"].sample_stride = 10
        waves["CH1"].device_record_length = 1000
        with self.assertRaisesRegex(ValueError, "抽点预览"):
            save_archive(self.path, waves, settings, require_complete=True)
        save_archive(self.path, waves, settings)
        restored, _, metadata = load_archive(self.path)
        self.assertFalse(restored["CH1"].full_record)
        self.assertEqual(restored["CH1"].sample_stride, 10)
        self.assertFalse(metadata["waveforms"]["CH1"]["full_record"])

    def test_adapter_record_depth_must_match_saved_frame_settings(self):
        waves, settings = capture()
        waves["CH1"].device_record_length = 2000
        waves["CH1"].sample_stride = 1
        waves["CH1"].full_record = True
        with self.assertRaisesRegex(ValueError, "不属于同一记录"):
            save_archive(self.path, waves, settings, require_complete=True)

    def test_adapter_provenance_round_trips_without_becoming_derived(self):
        waves, settings = capture()
        waves["CH1"].device_record_length = 1000
        waves["CH1"].sample_stride = 1
        waves["CH1"].full_record = True
        save_archive(self.path, waves, settings, require_complete=True)
        restored, loaded_settings, metadata = load_archive(self.path)
        self.assertTrue(restored["CH1"].full_record)
        self.assertEqual(restored["CH1"].device_record_length, 1000)
        self.assertEqual(metadata["waveforms"]["CH1"]["completeness_basis"], "adapter")
        second = self.path.with_name("second.npz")
        save_archive(second, restored, loaded_settings, require_complete=True)
        _, _, second_metadata = load_archive(second)
        self.assertEqual(metadata["acquisition"], second_metadata["acquisition"])

    def test_missing_or_disabled_channels_cannot_be_complete(self):
        waves, settings = capture(enabled=(1, 2))
        with self.assertRaisesRegex(ValueError, "缺少.*CH2"):
            save_archive(self.path, {"CH1": waves["CH1"]}, settings, require_complete=True)
        settings["channels"]["CH2"].enabled = False
        with self.assertRaisesRegex(ValueError, "未开启.*CH2"):
            save_archive(self.path, waves, settings, require_complete=True)

    def test_unknown_device_depth_is_never_claimed_complete(self):
        waves, settings = capture(depth=0)
        save_archive(self.path, waves, settings)
        _, _, meta = load_archive(self.path)
        self.assertEqual(meta["acquisition"]["completeness"], "unknown")
        self.assertIsNone(meta["waveforms"]["CH1"]["device_record_length"])
        with self.assertRaisesRegex(ValueError, "实际存储深度"):
            save_archive(self.path, waves, settings, require_complete=True)

    def test_unknown_sample_rate_cannot_be_strict_complete(self):
        waves, settings = capture()
        settings["timebase"].sample_rate = 0
        with self.assertRaisesRegex(ValueError, "实际采样率"):
            save_archive(self.path, waves, settings, require_complete=True)

    def test_adapter_full_record_with_missing_settings_still_loads_honestly(self):
        waves, settings = capture()
        waves["CH1"].device_record_length = 1000
        waves["CH1"].sample_stride = 1
        waves["CH1"].full_record = True
        settings["timebase"].sample_rate = 0
        settings["timebase"].record_length = 0
        save_archive(self.path, waves, settings)
        restored, _, metadata = load_archive(self.path)
        self.assertTrue(restored["CH1"].full_record)
        self.assertEqual(metadata["waveforms"]["CH1"]["device_record_length"], 1000)
        self.assertEqual(metadata["acquisition"]["completeness"], "unknown")

    def test_raw_codes_count_must_match_voltages(self):
        waves, settings = capture()
        waves["CH1"].raw = waves["CH1"].raw[:-1]
        with self.assertRaisesRegex(ValueError, "原始采样码点数"):
            save_archive(self.path, waves, settings, require_complete=True)

    def test_ascii_original_capture_can_be_complete_without_integer_codes(self):
        waves, settings = capture()
        waves["CH1"].raw = None
        waves["CH1"].source_encoding = "ASCII"
        save_archive(self.path, waves, settings, require_complete=True)
        restored, _, meta = load_archive(self.path)
        self.assertIsNone(restored["CH1"].raw)
        self.assertEqual(meta["acquisition"]["completeness"], "complete")

    def test_declared_point_count_is_checked_against_stored_array(self):
        waves, settings = capture()
        save_archive(self.path, waves, settings)
        rewrite(self.path, lambda meta, arrays: meta["waveforms"]["CH1"].update(points=10_000_000))
        with self.assertRaisesRegex(ValueError, "真实数组"):
            load_archive(self.path)

    def test_complete_declaration_cannot_hide_shortened_arrays(self):
        waves, settings = capture()
        save_archive(self.path, waves, settings, require_complete=True)
        def shorten(meta, arrays):
            arrays["wave_0"] = arrays["wave_0"][:100]
            arrays["raw_0"] = arrays["raw_0"][:100]
            meta["waveforms"]["CH1"]["points"] = 100
        rewrite(self.path, shorten)
        with self.assertRaisesRegex(ValueError, "真实性"):
            load_archive(self.path)

    def test_legacy_archive_remains_readable_but_completeness_unknown(self):
        waves, settings = capture()
        save_archive(self.path, waves, settings)
        def make_legacy(meta, arrays):
            meta.pop("acquisition")
            for info in meta["waveforms"].values():
                for key in ("points", "device_record_length", "sample_stride", "full_record",
                            "adapter_record_length", "adapter_sample_stride", "adapter_full_record",
                            "completeness_basis"):
                    info.pop(key, None)
        rewrite(self.path, make_legacy)
        restored, _, meta = load_archive(self.path)
        np.testing.assert_array_equal(restored["CH1"].y, waves["CH1"].y)
        self.assertEqual(meta["acquisition"]["completeness"], "unknown")

    def test_size_budget_is_configurable_and_large_records_have_room(self):
        # Four 10M point records: float64 voltage + int16 original code per channel.
        self.assertGreater(DEFAULT_MAX_UNCOMPRESSED_BYTES, 4 * 10_000_000 * (8 + 2))
        waves, settings = capture()
        save_archive(self.path, waves, settings)
        with zipfile.ZipFile(self.path) as archive:
            total_size = sum(entry.file_size for entry in archive.infolist())
        with self.assertRaisesRegex(ValueError, "解压后超过"):
            load_archive(self.path, max_uncompressed_bytes=total_size - 1)
        restored, _, _ = load_archive(self.path, max_uncompressed_bytes=total_size)
        self.assertEqual(restored["CH1"].points, 1000)

    def test_compression_failure_preserves_previous_file_and_cleans_temporary(self):
        waves, settings = capture()
        save_archive(self.path, waves, settings)
        before = self.path.read_bytes()
        with patch("scopeapp.archive.np.savez_compressed", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                save_archive(self.path, waves, settings, require_complete=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_nonfinite_native_samples_are_rejected_before_replacing_file(self):
        waves, settings = capture()
        save_archive(self.path, waves, settings)
        before = self.path.read_bytes()
        waves["CH1"].y[12] = np.nan
        with self.assertRaisesRegex(ValueError, "采样数据无效"):
            save_archive(self.path, waves, settings, require_complete=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_running_record_is_rejected_even_when_every_native_point_is_present(self):
        waves, settings = capture()
        for wf in waves.values():
            wf.record_stable = False
        with self.assertRaisesRegex(ValueError, "手动 STOP"):
            save_archive(self.path, waves, settings, require_complete=True)
        save_archive(self.path, waves, settings)
        restored, _, meta = load_archive(self.path)
        self.assertEqual(meta["acquisition"]["completeness"], "unstable")
        self.assertTrue(all(wf.record_stable is False for wf in restored.values()))

    def test_stable_record_provenance_survives_npz_round_trip(self):
        waves, settings = capture()
        for wf in waves.values():
            wf.record_stable = True
        save_archive(self.path, waves, settings, require_complete=True)
        restored, _, _ = load_archive(self.path)
        self.assertTrue(all(wf.record_stable is True for wf in restored.values()))

    def test_stale_waveform_points_field_does_not_inflate_archive_count(self):
        waves, settings = capture(points=100, depth=1000)
        waves["CH1"].points = 10_000_000
        save_archive(self.path, waves, settings)
        restored, _, meta = load_archive(self.path)
        self.assertEqual(restored["CH1"].points, 100)
        self.assertEqual(meta["waveforms"]["CH1"]["points"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
