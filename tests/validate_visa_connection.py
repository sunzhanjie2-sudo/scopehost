"""DS1152E 真实 USB 扫描→后台连接→采集→关闭验证；不调节设备通道和采集设置。"""
from __future__ import annotations
import argparse
import json
import multiprocessing as mp
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def settings(adapter):
    return {"channels": {f"CH{n}": asdict(adapter.read_channel(n)) for n in (1, 2)},
            "timebase": asdict(adapter.read_timebase()), "trigger": asdict(adapter.read_trigger()),
            "running": adapter.is_running(), "transfer_mode": adapter.engine.ask(":WAV:POIN:MODE?")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource", default="USB0::0x1AB1::0x0588::DS1EI182307208::INSTR")
    args = parser.parse_args()
    from PySide6.QtWidgets import QApplication
    from scopeapp import __version__
    from scopeapp.session import ScopeSession
    from scopeapp.visa_process import VisaProcessTransport
    from scopeapp.ui.main_window import MainWindow, ConnectDialog

    out = ROOT / "artifacts"
    os.environ["SCOPEHOST_LOG"] = str(out / "visa_usb_v0.6.1_scpi.log")
    report = {"version": __version__, "hardware": "DS1152E", "simulation": False}
    scan = VisaProcessTransport().scan_report()
    assert args.resource in scan["usb_resources"], scan
    report["scan"] = scan
    session = ScopeSession()
    try:
        adapter = session.connect_visa(args.resource)
        assert adapter.identity.model == "DS1152E", adapter.identity.full
        report["identity"] = adapter.identity.full
        report["before"] = settings(adapter)
    finally:
        session.disconnect()
    app = QApplication([])
    errors = []
    original_hook = sys.excepthook
    sys.excepthook = lambda kind, value, tb: errors.append(str(value))
    window = MainWindow(visa_resource=args.resource)
    window.show()
    try:
        deadline = time.monotonic() + 12
        while window._frames < 3 and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert not errors, errors
        assert window._frames >= 3, (window.lbl_status.text(), window.statusBar().currentMessage())
        assert window._sim is None and "真实设备" in window.lbl_source.text()
        assert window.session.adapter.identity.model == "DS1152E"
        assert not window._ch_widgets[3]["group"].isVisible()
        assert not window._ch_widgets[4]["group"].isVisible()
        expected = {name for name, ch in window._settings["channels"].items() if ch.enabled}
        assert set(window._raw_waveforms) == expected
        assert all(wf.points == 600 for wf in window._raw_waveforms.values())
        report["frames"] = window._frames
        report["waveforms"] = {name: {"points": wf.points, "vpp": wf.vpp, "duration": wf.duration} for name, wf in window._raw_waveforms.items()}
        assert window.grab().save(str(out / "ds1152e_visa_v0.6.1.png"))
        dialog = ConnectDialog(window)
        dialog.show()
        deadline = time.monotonic() + 10
        while (not dialog._scan_report or (dialog._scanner and dialog._scanner.isRunning())) and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert args.resource in dialog._scan_report.get("resources", []), dialog.tip.text()
        assert dialog.selected()["visa"] == args.resource
        assert dialog.grab().save(str(out / "visa_dialog_v0.6.1.png"))
        report["gui_scan"] = dialog._scan_report
        dialog.reject()
        assert window.session.connected, "扫描不能关闭正在采集的 VISA 会话"
    finally:
        window.close()
        app.processEvents()
        sys.excepthook = original_hook
    session = ScopeSession()
    try:
        report["after"] = settings(session.connect_visa(args.resource))
    finally:
        session.disconnect()
    assert report["after"] == report["before"], "设备通道、时基、触发或传输模式发生变化"
    report["passed"] = True
    report["settings_unchanged"] = True
    (out / "visa_ds1152e_validation_v0.6.1.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    mp.freeze_support()
    main()
