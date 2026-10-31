"""Hardware acceptance for full native MSO44 records; restore current user settings."""
from pathlib import Path
from dataclasses import replace
import json
import multiprocessing as mp
import os
import runpy
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
RESOURCE = "USB0::0x0699::0x0527::C049800::INSTR"


def main():
    import numpy as np
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
    from scopeapp.session import ScopeSession
    from scopeapp.archive import save_archive, load_archive
    from scopeapp.scpi import parse_ieee_block
    from scopeapp.adapters.tektronix import parse_wfmoutpre
    from scopeapp.ui.main_window import MainWindow
    from scopeapp.waveform import sample_bounds
    from scopeapp import __version__

    helpers = runpy.run_path(str(ROOT / "tests/validate_mso44.py"))
    controls, same, events, pump = (helpers[name] for name in ("controls", "same", "event_status", "pump"))
    out = ROOT / "artifacts" / "mso44_v0.6.3"
    out.mkdir(parents=True, exist_ok=True)
    os.environ["SCOPEHOST_LOG"] = str(out / "scpi.log")
    report = {"version": __version__, "resource": RESOURCE, "simulation": False}
    session, window, before, protocol = ScopeSession(), None, {}, {}
    app = QApplication.instance() or QApplication([])
    try:
        ad = session.connect_visa(RESOURCE)
        engine = ad.engine
        report["identity"] = ad.identity.full
        before = controls(engine)
        protocol = {cmd: engine.ask(cmd + "?").strip() for cmd in
                    (":DATA:SOURCE", ":DATA:START", ":DATA:STOP", ":DATA:RESAMPLE", ":DATA:ENCDG", ":DATA:WIDTH")}
        report["before"] = before
        report["protocol_before"] = protocol
        (out / "current_settings_before.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        for n in range(1, 5):
            engine.write(f":DISPLAY:GLOBAL:CH{n}:STATE ON")
        # Newly enabled channels need an acquisition before their stored data exists.
        engine.write(":ACQUIRE:STOPAFTER RUNSTOP;:ACQUIRE:STATE RUN")
        ready_deadline = time.monotonic() + 10
        while time.monotonic() < ready_deadline:
            sources = {value.strip().upper() for value in engine.ask(":DATA:SOURCE:AVAILABLE?").split(",")}
            if {"CH1", "CH2", "CH3", "CH4"}.issubset(sources):
                break
            time.sleep(.1)
        else:
            raise RuntimeError("四通道尚未产生完整采集记录")
        engine.write(":ACQUIRE:STATE STOP")
        assert engine.ask("*OPC?").strip() == "1"
        settings = {}

        def snapshot():
            settings.update(channels={f"CH{n}": ad.read_channel(n) for n in range(1, 5)},
                            timebase=ad.read_timebase(), trigger=ad.read_trigger())
            ad.update_enabled_cache(settings["channels"])

        ad.frame_settings_callback = snapshot
        ad.waveform_progress = lambda p: print(f"{p['channel']}: {p['channel_points']:,}/{p['record_points']:,}", flush=True) if p['channel_points'] == p['record_points'] else None
        start = time.monotonic()
        waveforms = ad.fetch_all()
        assert set(waveforms) == {"CH1", "CH2", "CH3", "CH4"}
        tb = settings["timebase"]
        for wf in waveforms.values():
            assert wf.points == wf.y.size == wf.raw.size == tb.record_length == wf.device_record_length
            assert wf.full_record is True and wf.sample_stride == 1
            assert abs(wf.x_incr * tb.sample_rate - 1) < 1e-8
        report["full_read"] = {"elapsed_ms": (time.monotonic() - start) * 1000,
                               "depth": tb.record_length, "sample_rate": tb.sample_rate,
                               "points": {name: wf.points for name, wf in waveforms.items()}}
        wf = waveforms["CH1"]
        threshold = (np.max(wf.y) + np.min(wf.y)) / 2
        rises = np.flatnonzero((wf.y[:-1] < threshold) & (wf.y[1:] >= threshold))
        edge = int(rises[len(rises)//2])
        ranges = [(0, min(1024, wf.points)), (max(0, edge-512), min(edge+512, wf.points)), (wf.points-1024, wf.points)]
        comparisons = []
        for first, last in ranges:
            engine.write(f":DATA:SOURCE CH1;:DATA:RESAMPLE 1;:DATA:START {first+1};:DATA:STOP {last}")
            engine.write(":HEADER ON")
            pre = parse_wfmoutpre(engine.ask(":WFMOUTPRE?"))
            engine.write(":HEADER OFF")
            raw_bytes, _ = parse_ieee_block(engine.ask_binary(":CURVE?"))
            direct = ad._decode(raw_bytes, pre, 1)
            assert np.array_equal(direct.raw, wf.raw[first:last])
            assert np.array_equal(direct.y, wf.y[first:last])
            assert abs(direct.x_start - (wf.x_start+first*wf.x_incr)) < wf.x_incr*.01
            comparisons.append({"first_index": first, "points": last-first, "raw_and_voltage_equal": True})
        report["independent_curve_comparisons"] = comparisons
        archive_path = out / "full_four_channels.npz"
        start = time.monotonic()
        save_archive(archive_path, waveforms, settings, ad.identity.full, require_complete=True)
        loaded, loaded_settings, meta = load_archive(archive_path)
        for name, original in waveforms.items():
            assert np.array_equal(original.raw, loaded[name].raw)
            assert np.array_equal(original.y, loaded[name].y)
            assert loaded[name].full_record is True and loaded[name].points == tb.record_length
        report["npz"] = {"elapsed_ms": (time.monotonic()-start)*1000, "bytes": archive_path.stat().st_size,
                         "every_raw_and_voltage_sample_equal": True, "points": {name: value.points for name, value in loaded.items()}}
        del loaded, waveforms, wf, direct
        ad.single()
        assert pump(lambda: not ad.is_running(), 8), "Single did not complete"
        single_waveforms = ad.fetch_all()
        assert all(wf.points == tb.record_length and wf.full_record is True for wf in single_waveforms.values())
        assert not ad.is_running()
        assert engine.ask(":ACQUIRE:STOPAFTER?").strip().upper() == "SEQUENCE"
        report["single"] = {"points": {name: wf.points for name, wf in single_waveforms.items()}, "remained_stopped": True}
        del single_waveforms
        for n in range(1, 5):
            engine.write(f":DISPLAY:GLOBAL:CH{n}:STATE {before[f':DISPLAY:GLOBAL:CH{n}:STATE']}")
        engine.write(f":ACQUIRE:STOPAFTER {before[':ACQUIRE:STOPAFTER']}")
        engine.write(f":ACQUIRE:STATE {before[':ACQUIRE:STATE']}")
        session.disconnect()

        errors, beats = [], []
        timer = QTimer()
        timer.timeout.connect(lambda: beats.append(time.monotonic()))
        timer.start(20)
        with patch.object(QMessageBox, "warning", side_effect=lambda *args: errors.append(str(args[-1]))):
            window = MainWindow()
            window.resize(1450, 970)
            window.show()
            window._do_connect({"method": 2, "visa": RESOURCE, "adapter": None, "visa_backend": ""})
            assert pump(lambda: window._frames >= 1 or errors, 45, app), "GUI no full frame"
            assert not errors, errors
            assert all(value.points == window._frame_settings["timebase"].record_length for value in window._raw_waveforms.values())
            window.spn_refresh.setValue(10000)
            display_wf = window._raw_waveforms["CH1"]
            threshold = (display_wf.y.max()+display_wf.y.min()) / 2
            edges = np.flatnonzero((display_wf.y[:-1] < threshold) & (display_wf.y[1:] >= threshold))
            point = int(edges[len(edges)//2])
            window.btn_zoom.setChecked(True)
            window.spn_zoom_factor.setValue(200000)
            window.spn_zoom_center.setValue(display_wf.x_start + point*display_wf.x_incr)
            first, last = sample_bounds(display_wf, *window.scope_view.zoom_range)
            assert 2 <= last-first <= 1000
            exported = window._zoom_waveforms()["CH1"]
            assert np.array_equal(exported.raw, display_wf.raw[first:last])
            assert np.array_equal(exported.y, display_wf.y[first:last])
            app.processEvents()
            assert window.grab().save(str(out / "deep_zoom.png"))
            saved_values = display_wf.raw.copy()
            gui_path = out / "gui_full_record.npz"
            window._path = lambda *args: str(gui_path)
            heartbeat_before = len(beats)
            start = time.monotonic()
            window._save_archive()
            assert window._archive_writer is not None
            assert pump(lambda: window._archive_writer is None or errors, 60, app)
            assert not errors, errors
            loaded, _, _ = load_archive(gui_path)
            assert np.array_equal(loaded["CH1"].raw, saved_values)
            assert loaded["CH1"].points == display_wf.points
            report["gui"] = {"frames": window._frames, "zoom_points": last-first,
                             "zoom_raw_and_voltage_equal": True, "npz_points": loaded["CH1"].points,
                             "save_ms": (time.monotonic()-start)*1000, "heartbeats_during_save": len(beats)-heartbeat_before,
                             "max_heartbeat_gap_ms": max(np.diff(beats))*1000 if len(beats)>1 else None,
                             "zoom_label": window.lbl_zoom_info.text()}
            assert len(beats)-heartbeat_before > 2
            cancellation = {}

            def disconnect_during_block(progress):
                if cancellation or progress["channel_points"] >= progress["record_points"]:
                    return
                cancellation["at_points"] = progress["channel_points"]
                started = time.monotonic()
                cancellation["success"] = window._disconnect()
                cancellation["elapsed_ms"] = (time.monotonic()-started)*1000

            window.worker.waveformProgress.connect(disconnect_during_block)
            window.spn_refresh.setValue(100)
            assert pump(lambda: "success" in cancellation, 45, app), "No mid-record disconnect"
            assert cancellation["success"]
            report["gui"]["mid_record_disconnect"] = cancellation
            previous_frame = window._raw_waveforms["CH1"]
            window._do_connect({"method": 2, "visa": RESOURCE, "adapter": None, "visa_backend": ""})
            assert pump(lambda: (window.session.connected and window.worker is not None
                                and window._raw_waveforms.get("CH1") is not None
                                and window._raw_waveforms["CH1"] is not previous_frame) or errors, 45, app), "No complete frame after reconnect"
            assert not errors, errors
            assert window._raw_waveforms["CH1"].points == display_wf.points
            report["gui"]["full_frame_after_reconnect"] = True
            assert window._disconnect()
            window.close()
            window = None
        report["passed"] = True
    except Exception as error:
        import traceback
        traceback.print_exc()
        report.update(passed=False, error=str(error))
    finally:
        if window:
            if window.connector and window.connector.isRunning():
                window.connector.cancel()
                pump(lambda: not window.connector.isRunning(), 8, app)
            window._disconnect()
            window.close()
        if before:
            session.disconnect()
            session.connect_visa(RESOURCE)
            engine = session.engine
            engine.write(":HEADER OFF")
            for n in range(1, 5):
                engine.write(f":DISPLAY:GLOBAL:CH{n}:STATE {before[f':DISPLAY:GLOBAL:CH{n}:STATE']}")
            for cmd, value in protocol.items():
                engine.write(f"{cmd} {value}")
            engine.write(f":ACQUIRE:STOPAFTER {before[':ACQUIRE:STOPAFTER']}")
            engine.write(f":ACQUIRE:STATE {before[':ACQUIRE:STATE']}")
            after = controls(engine)
            report["restored"] = all(same(value, after[key]) for key, value in before.items())
            report["events"] = events(engine)
        session.disconnect()
        (out / "full_record_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key,value in report.items() if key!='before'}, ensure_ascii=False, indent=2), flush=True)
    return 0 if report.get("passed") and report.get("restored") and report.get("events", {}).get("esr", 60)&60 == 0 else 1


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
