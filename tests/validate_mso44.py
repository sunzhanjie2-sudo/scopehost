"""Attached MSO44 integration checks. Restores scope and transfer settings in finally."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from io import BytesIO
import json
import multiprocessing as mp
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pump(predicate, timeout=10, app=None):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if app:
            app.processEvents()
        if predicate():
            return True
        time.sleep(.01 if app else .1)
    return False


def controls(engine):
    commands = [":ACQUIRE:STATE", ":ACQUIRE:STOPAFTER", ":ACQUIRE:MODE",
        ":HORIZONTAL:MODE", ":HORIZONTAL:SCALE", ":HORIZONTAL:POSITION", ":HORIZONTAL:DELAY:MODE",
        ":HORIZONTAL:DELAY:TIME", ":HORIZONTAL:RECORDLENGTH", ":HORIZONTAL:SAMPLERATE",
        ":TRIGGER:A:TYPE", ":TRIGGER:A:EDGE:SOURCE", ":TRIGGER:A:EDGE:SLOPE", ":TRIGGER:A:MODE"]
    for number in range(1, 5):
        commands += [f":CH{number}:{field}" for field in ("SCALE", "OFFSET", "POSITION", "COUPLING", "BANDWIDTH")]
        commands += [f":DISPLAY:GLOBAL:CH{number}:STATE", f":TRIGGER:A:LEVEL:CH{number}"]
    return {command: engine.ask(command + "?").strip() for command in commands}


def event_status(engine):
    return {"esr": int(engine.ask("*ESR?")), "events": engine.ask(":ALLEV?")}


def same(first, second):
    try:
        return abs(float(first) - float(second)) <= max(1e-12, abs(float(first)) * 1e-8)
    except ValueError:
        return first.upper() == second.upper()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource", default="USB0::0x0699::0x0527::C049800::INSTR")
    parser.add_argument("--frames", type=int, default=10)
    args = parser.parse_args()
    if args.frames < 2:
        parser.error("--frames must be at least 2")
    from PIL import Image
    import numpy as np
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from scopeapp import __version__
    from scopeapp.archive import load_archive
    from scopeapp.measurements import measure
    from scopeapp.session import ScopeSession
    from scopeapp.visa_process import VisaProcessTransport
    from scopeapp.ui.main_window import MainWindow, ConnectDialog

    out = ROOT / "artifacts" / "mso44_v0.6.2"
    out.mkdir(parents=True, exist_ok=True)
    os.environ["SCOPEHOST_LOG"] = str(out / "scpi.log")
    report = {"version": __version__, "resource": args.resource, "simulation": False, "stage": "connect"}
    protocol, original = {}, {}
    session, raw, window, app = ScopeSession(), VisaProcessTransport(args.resource), None, None
    try:
        raw.connect()
        report["identity"] = raw.ask("*IDN?")
        assert ",MSO44," in report["identity"], report["identity"]
        for command in (":HEADER", ":VERBOSE"):
            protocol[command] = raw.ask(command + "?").strip().split(" ")[-1]
        raw.write(":HEADER OFF")
        for command in (":DATA:SOURCE", ":DATA:START", ":DATA:STOP", ":DATA:RESAMPLE", ":DATA:ENCDG", ":DATA:WIDTH"):
            protocol[command] = raw.ask(command + "?").strip()
        original = controls(raw)
        report["before"] = original
        report["protocol_before"] = dict(protocol)
        report["existing_events"] = event_status(raw)
        raw.disconnect()
        ad = session.connect_visa(args.resource)
        assert ad.identity.model == "MSO44" and ad.channel_count == 4
        report["stage"] = "native_capture"
        image = ad.capture_screen()
        with Image.open(BytesIO(image)) as native:
            native.load()
            report["native_screen"] = {"format": native.format, "size": list(native.size), "bytes": len(image)}
        (out / "native_initial.png").write_bytes(image)
        status = event_status(ad.engine)
        report["native_capture_events"] = status
        assert not status["esr"] & 0x3c, status

        report["stage"] = "basic_controls"
        before_scale = float(original[":CH1:SCALE"])
        before_offset = float(original[":CH1:OFFSET"])
        ad.set_channel_scale(1, before_scale * 2)
        assert same(ad.read_channel(1).scale, before_scale * 2)
        ad.set_channel_scale(1, before_scale)
        ad.set_channel_offset(1, before_offset + before_scale / 4)
        assert same(ad.read_channel(1).offset, before_offset + before_scale / 4)
        ad.set_channel_offset(1, before_offset)
        ad.set_channel_coupling(1, "AC")
        assert ad.read_channel(1).coupling.value == "AC"
        ad.set_channel_coupling(1, original[":CH1:COUPLING"])
        ad.engine.write(":CH1:BANDWIDTH 20E6")
        assert ad.read_channel(1).bandwidth == "20 MHz"
        ad.engine.write(f':CH1:BANDWIDTH {original[":CH1:BANDWIDTH"]}')
        scale = float(original[":HORIZONTAL:SCALE"])
        ad.set_timebase(scale * 2)
        assert same(ad.read_timebase().scale, scale * 2)
        ad.set_timebase(scale)
        ad.engine.write(":HORIZONTAL:DELAY:MODE OFF;:HORIZONTAL:POSITION 75")
        time.sleep(1)
        tb, wf = ad.read_timebase(), ad.fetch_waveform(1)
        assert same(tb.position, -.25 * tb.scale * tb.horizontal_divisions)
        assert abs(wf.x[0] + wf.duration / 2 - tb.position) < wf.x_incr * 2
        report["horizontal_75"] = {"position": tb.position, "waveform_center": float(wf.x[0] + wf.duration / 2)}
        ad.engine.write(":HORIZONTAL:DELAY:MODE ON;:HORIZONTAL:DELAY:TIME 0.002")
        time.sleep(1)
        tb = ad.read_timebase()
        assert same(tb.position, .002), asdict(tb)
        report["delay_2ms"] = asdict(tb)
        ad.engine.write(f':HORIZONTAL:DELAY:TIME {original[":HORIZONTAL:DELAY:TIME"]};'
                        f':HORIZONTAL:POSITION {original[":HORIZONTAL:POSITION"]};'
                        f':HORIZONTAL:DELAY:MODE {original[":HORIZONTAL:DELAY:MODE"]}')
        ad.set_trigger_source("CH1")
        ad.set_trigger_level(.8)
        assert abs(ad.read_trigger().level - .8) <= before_scale / 50, "Trigger level exceeds hardware quantization tolerance"
        ad.set_trigger_mode("NORMAL")
        assert ad.read_trigger().mode == "NORMAL"
        ad.set_trigger_slope("FALL")
        assert ad.read_trigger().slope == "FALL"
        ad.set_trigger_slope(original[":TRIGGER:A:EDGE:SLOPE"])
        ad.set_trigger_mode(original[":TRIGGER:A:MODE"])
        for number in range(1, 5):
            assert ad.set_channel_enabled(number, True), f"CH{number} did not enable"
        assert pump(lambda: set(ad.engine.ask(":DATA:SOURCE:AVAILABLE?").strip().split(",")) >= {"CH1", "CH2", "CH3", "CH4"}, 5)
        channels = {f"CH{n}": ad.read_channel(n) for n in range(1, 5)}
        ad.update_enabled_cache(channels)
        report["stage"] = "preview_profiles"
        print("MSO44: controls passed; measuring four-channel preview profiles", flush=True)
        profiles = []
        for limit in (10000, 20000, 50000, 100000):
            ad.set_preview_points(limit)
            tb = ad.read_timebase()
            started = time.monotonic()
            waveforms = ad.fetch_all()
            assert set(waveforms) == {"CH1", "CH2", "CH3", "CH4"}
            assert all(wf.points <= limit and np.isfinite(wf.y).all() for wf in waveforms.values())
            assert all(abs(wf.duration - tb.record_length / tb.sample_rate) <= wf.x_incr * 2 for wf in waveforms.values())
            assert tb.record_length == int(original[":HORIZONTAL:RECORDLENGTH"])
            profiles.append({"limit": limit, "frame_ms": (time.monotonic() - started) * 1000,
                             "points": {name: wf.points for name, wf in waveforms.items()}})
            print(f"MSO44: {limit} points/channel, {profiles[-1]['frame_ms']:.0f} ms/frame", flush=True)
        report["profiles"] = profiles
        ad.set_preview_points(10000)
        ad.stop()
        assert not ad.is_running()
        ad.single()
        assert pump(lambda: not ad.is_running(), 6), "Single did not finish"
        frozen = ad.fetch_all()
        assert len(frozen) == 4
        report["single_completed"] = True
        ad.run()
        assert ad.is_running()
        status = event_status(ad.engine)
        report["control_events"] = status
        assert not status["esr"] & 0x3c, status
        session.disconnect()

        report["stage"] = "gui"
        # 深记录的设备抽取耗时不随 USB 传输点数同比下降。
        # 验证期限包含实测每帧耗时和帧后延迟，同时持续处理 Qt 事件。
        frame_budget = max(20, max(item["frame_ms"] for item in profiles) / 1000 + 8)
        command_budget = frame_budget * 2
        report["gui_frame_budget_s"] = frame_budget
        print(f"MSO44: GUI acquisition, {args.frames} frames, {frame_budget:.1f} s budget/frame", flush=True)
        app = QApplication([])
        errors, ticks, timings = [], [], []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(20)
        window = MainWindow()
        original_error = window._acquisition_error
        window._acquisition_error = lambda message: (errors.append(message), original_error(message))
        window.show()
        with patch("scopeapp.ui.main_window.QMessageBox.warning", side_effect=lambda *items: errors.append(items[-1])):
            dialog = ConnectDialog(window)
            dialog.show()
            assert pump(lambda: bool(dialog._scan_report) and not dialog._scanner.isRunning(), 12, app), "GUI scan deadline"
            assert args.resource in dialog._scan_report.get("resources", []), dialog._scan_report
            dialog.visa_res.setEditText(args.resource)
            options = dialog.selected()
            report["gui_scan"] = dialog._scan_report
            dialog.reject()
            window._do_connect(options)
            assert pump(lambda: window._frames > 0 or bool(errors), frame_budget + 15, app), window.lbl_status.text()
            assert not errors, errors
            window.worker.dataReady.connect(lambda payload: timings.append(payload["acquisition_ms"]))
            assert pump(lambda: window._frames >= args.frames or bool(errors), frame_budget * args.frames, app), window.statusBar().currentMessage()
            assert not errors, errors
            report["continuous_frames"] = window._frames
            assert window._sim is None and "真实设备" in window.lbl_source.text()
            assert set(window._raw_waveforms) == {"CH1", "CH2", "CH3", "CH4"}
            assert window.grab().save(str(out / "gui_four_channels.png"))
            print(f"MSO44: {window._frames} real GUI frames received; testing GUI controls and exports", flush=True)
            offset = window._ch_widgets[1]["offset"]
            offset.setValue(before_offset + before_scale / 5)
            assert pump(lambda: offset not in window._pending_numeric and same(window._settings["channels"]["CH1"].offset, before_offset + before_scale / 5), command_budget, app), "GUI offset apply deadline"
            offset.setValue(before_offset)
            assert pump(lambda: offset not in window._pending_numeric and same(window._settings["channels"]["CH1"].offset, before_offset), command_budget, app), "GUI offset restore deadline"
            window.btn_zoom.setChecked(True)
            window.btn_stacked.setChecked(True)
            window.btn_cursor.setChecked(True)
            window.cmb_cursor_scope.setCurrentIndex(1)
            window.btn_fft.setChecked(True)
            app.processEvents()
            assert window.grab().save(str(out / "gui_zoom_stacked.png"))
            with patch.object(window, "_path", return_value=str(out / "software_window.png")):
                window._save_plot()
            with Image.open(out / "software_window.png") as software:
                software.load()
                assert software.width == window.width() and software.height == window.height()
            with patch.object(window, "_path", return_value=str(out / "capture.npz")):
                window._save_archive()
            replay, _, metadata = load_archive(out / "capture.npz")
            assert set(replay) == set(window._raw_waveforms) and "MSO44" in metadata["identity"]
            with patch.object(window, "_path", return_value=str(out / "native_gui.png")):
                (out / "native_gui.png").unlink(missing_ok=True)
                window._on_screenshot()
            assert pump(lambda: (out / "native_gui.png").exists() and not window._shot_path, command_budget, app), "GUI native capture deadline"
            with Image.open(out / "native_gui.png") as native:
                native.load()
                assert native.size == (1920, 1080)
            window.btn_single.click()
            assert pump(lambda: window._state == "SINGLE DONE" or bool(errors), command_budget, app), window.lbl_state.text()
            assert not errors, errors
            report["gui_single_completed"] = True
            # 在真实 CURVE 回传期间断开，随后从同一 USB 接口重连。
            # 只观察操作，不缩短读取、不注入模拟数据。
            in_binary = threading.Event()
            transport = window.session.engine.transport
            ask_binary = transport.ask_binary
            def observed_binary(command):
                curve = command.strip().lstrip(":").upper() == "CURVE?"
                if curve:
                    in_binary.set()
                try:
                    return ask_binary(command)
                finally:
                    if curve:
                        in_binary.clear()
            transport.ask_binary = observed_binary
            window._submit("run")
            assert pump(in_binary.is_set, frame_budget, app), "No real CURVE in flight"
            print("MSO44: testing disconnect during real CURVE transfer and USB reconnect", flush=True)
            started = time.monotonic()
            assert window._disconnect(), "Graceful disconnect failed"
            report["disconnect_during_curve_ms"] = (time.monotonic() - started) * 1000
            assert not errors, errors
            window._do_connect(options)
            assert pump(lambda: window.worker is not None or bool(errors), 15, app), "Reconnect deadline"
            reconnected_at_frame = window._frames
            assert pump(lambda: window._frames > reconnected_at_frame or bool(errors), frame_budget, app), "Reconnect frame deadline"
            assert not errors, errors
            report["usb_reconnect_passed"] = True
            report["gui"] = {"frames": report["continuous_frames"], "reconnected_frames": window._frames, "errors": errors,
                "max_event_gap_ms": max(np.diff(ticks), default=0) * 1000,
                "acquisition_ms": timings, "refresh_delay_ms": window.spn_refresh.value(),
                "waveforms": {name: {"points": wf.points, "vpp": wf.vpp,
                    "measurements": {item.name: item.value for item in measure(wf)}}
                    for name, wf in window._raw_waveforms.items()}}
            assert report["gui"]["max_event_gap_ms"] < 1500
        timer.stop()
        report["passed"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        report["passed"] = False
    finally:
        if window:
            window.close()
            if app:
                pump(lambda: window.worker is None, 8, app)
        session.disconnect()
        raw.disconnect()
        if original:
            restore = ScopeSession()
            try:
                ad = restore.connect_visa(args.resource)
                ad.stop()
                for command, value in original.items():
                    if command not in (":ACQUIRE:STATE", ":HORIZONTAL:MODE", ":HORIZONTAL:RECORDLENGTH", ":HORIZONTAL:SAMPLERATE"):
                        ad.engine.write(f"{command} {value}")
                ad.engine.write(f':ACQUIRE:STATE {original[":ACQUIRE:STATE"]}')
                ad.engine.ask("*OPC?")
                time.sleep(1)
                report["after"] = controls(ad.engine)
                changed = {key: [value, report["after"][key]] for key, value in original.items() if not same(value, report["after"][key])}
                report["settings_restored"] = not changed
                report["restore_differences"] = changed
                for command, value in protocol.items():
                    if command not in (":HEADER", ":VERBOSE"):
                        ad.engine.write(f"{command} {value}")
                report["protocol_after"] = {command: ad.engine.ask(command + "?").strip() for command in protocol if command not in (":HEADER", ":VERBOSE")}
                report["final_events"] = event_status(ad.engine)
                assert not report["final_events"]["esr"] & 0x3c, report["final_events"]
                for command in (":VERBOSE", ":HEADER"):
                    ad.engine.write(f"{command} {protocol[command]}")
                report["protocol_restored"] = all(same(value, report["protocol_after"][command]) for command, value in protocol.items() if command not in (":HEADER", ":VERBOSE"))
            except Exception as error:
                report["restore_error"] = str(error)
            finally:
                restore.disconnect()
        report["passed"] = bool(report.get("passed") and report.get("settings_restored") and report.get("protocol_restored") and not report.get("restore_error"))
        (out / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
