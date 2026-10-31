"""真机验收：只连接真实 DS1152E，不允许模拟器回退。

需要 CH1 接校准端、CH2 悬空且两通道已开启。会调节刻度/触发，并在最后
恢复进入测试时的量程、时基、耦合和触发设置，保持连续运行。
"""
from pathlib import Path
from dataclasses import asdict
import os
import sys
import time
import json
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "artifacts"
OUT.mkdir(exist_ok=True)
os.environ["SCOPEHOST_LOG"] = str(OUT / "ds1152e_scpi.log")
import numpy as np
from PySide6.QtWidgets import QApplication, QTabWidget
from PySide6.QtTest import QTest
from scopeapp.ui.main_window import MainWindow
from scopeapp.transport import list_visa_resources
from scopeapp.adapters.rigol_ds1000e import RigolDs1000eAdapter
from scopeapp.archive import save_archive, load_archive
from scopeapp.export import export_spreadsheet
from scopeapp.measurements import measure

app = QApplication([])
errors, checks = [], []
sys.excepthook = lambda kind, value, trace: errors.append(str(value))
resources = [r for r in list_visa_resources() if "0x1AB1" in r and "0x0588" in r]
assert len(resources) == 1, f"需要一台真实 DS1000E，发现 {resources}"
window = MainWindow(visa_resource=resources[0])
window.show()

def until(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if errors: raise AssertionError(errors)
        if predicate(): return
        # QtTest 的 qWait 会占用 GIL，影响 Python 通信线程的实际帧率。
        time.sleep(0.02)
    raise AssertionError(f"等待超时，状态 {window._state}，{window.statusBar().currentMessage()}")

def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)

def check(name, condition, detail=""):
    assert condition, f"{name}: {detail}"
    checks.append({"check": name, "detail": detail})
    print("PASS", name, detail, flush=True)

def control(name, args, predicate):
    old_frame = window._frames
    window._submit(name, *args)
    until(lambda: window._frames > old_frame and predicate())
    check(f"控制 {name} {args}", True)

baseline = None
try:
    until(lambda: window._frames >= 3)
    adapter = window.session.adapter
    check("真实设备身份", isinstance(adapter, RigolDs1000eAdapter) and adapter.identity.model == "DS1152E", adapter.identity.full)
    check("真实数据来源", window._sim is None and "真实设备" in window.lbl_source.text())
    check("只有两个通道", set(window._raw_waveforms) == {"CH1", "CH2"})
    check("隐藏 CH3/CH4 控件", window._ch_widgets[3]["group"].isHidden() and window._ch_widgets[4]["group"].isHidden())
    for widget in (window.cmb_trig_src, window.cmb_math_a, window.cmb_math_b, window.cmb_analysis_src):
        check("选择器不包含不存在的通道", all(widget.itemText(i) not in {"CH3", "CH4"} for i in range(widget.count())))
    baseline = {"channels": dict(window._settings["channels"]), "timebase": window._settings["timebase"], "trigger": window._settings["trigger"]}
    for scale in (.0005, .0002):
        control("set_timebase", (scale,), lambda s=scale: window._settings["timebase"].scale == s and np.isclose(window._raw_waveforms["CH1"].duration, 12*s))
        values = {m.name:m.value for m in measure(window._raw_waveforms["CH1"])}
        check("不同时基均测得 1 kHz", abs(values["Freq"]-1000)<10, str(values["Freq"]))
    for scale in (.5, 2, 1):
        control("set_channel_scale", (1, scale), lambda s=scale: window._settings["channels"]["CH1"].scale == s)
        check("刻度变化后电压幅值不变", abs(window._raw_waveforms["CH1"].vpp-3)<.15, str(window._raw_waveforms["CH1"].vpp))
    for offset in (0, -1.5):
        control("set_channel_offset", (1, offset), lambda o=offset: window._settings["channels"]["CH1"].offset == o)
        wf=window._raw_waveforms["CH1"]
        check("偏移变化后输入电压仍为 0～3 V", abs(wf.y.min())<.15 and abs(wf.y.max()-3)<.15)
    for coupling in ("AC", "DC"):
        control("set_channel_coupling", (1,coupling), lambda c=coupling: window._settings["channels"]["CH1"].coupling.value == c)
    for slope in ("FALL", "RISE"):
        control("set_trigger_slope", (slope,), lambda s=slope: window._settings["trigger"].slope == s)
    for mode in ("NORMAL", "AUTO"):
        control("set_trigger_mode", (mode,), lambda m=mode: window._settings["trigger"].mode.startswith(m[:4]))
    window._submit("stop")
    until(lambda: window._state == "STOP")
    pump(.6)
    frozen = {n:w.y.copy() for n,w in window._raw_waveforms.items()};frames=window._frames
    pump(.8)
    check("停止后波形冻结", window._frames == frames and all(np.array_equal(window._raw_waveforms[n].y,y) for n,y in frozen.items()))
    window._submit("run");until(lambda: window._state == "RUN" and window._frames>frames)
    check("重新运行继续采集", True)
    window._on_single();until(lambda: window._state == "SINGLE DONE")
    frozen={n:w.y.copy() for n,w in window._raw_waveforms.items()};frames=window._frames
    pump(.8)
    check("Single 同一冻结记录读取两通道", set(frozen)=={"CH1","CH2"} and frames == window._frames)
    window._submit("run");until(lambda: window._state == "RUN")
    control("set_trigger_level", (6,), lambda: window._settings["trigger"].level == 6)
    window._on_single();until(lambda: window._state == "ARMED")
    pump(.8)
    check("无有效边沿时 Single 等待", window._state == "ARMED")
    window._submit("force_trigger");until(lambda: window._state == "SINGLE DONE")
    check("强制触发完成", True)
    window._submit("run");until(lambda: window._state == "RUN")
    control("set_trigger_level", (1.5,), lambda: window._settings["trigger"].level == 1.5)
    window._on_single();until(lambda: window._state == "SINGLE DONE")
    window.btn_cursor.setChecked(True)
    window.scope_view.cursor_times = [0,.0005]
    window._analysis_options()
    until(lambda: len(window._raw_waveforms)==2)
    ch1=window._raw_waveforms["CH1"];ch2=window._raw_waveforms["CH2"]
    local={n:{m.name:m.value for m in measure(w)} for n,w in window._raw_waveforms.items()}
    device={q:float(adapter.engine.ask(f":MEAS:{q}? CHAN1")) for q in ("VPP","VMAX","VMIN","FREQ")}
    check("USB 数据与仪器内置测量一致", abs(local["CH1"]["Vpp"]-device["VPP"])<.1 and abs(local["CH1"]["Freq"]-device["FREQ"])<5, json.dumps({"local":local,"device":device}))
    check("CH2 仅底噪且不报告虚假周期", ch2.vpp<.03 and local["CH2"]["Freq"]==0, str(local["CH2"]))
    path=save_archive(OUT/"ds1152e_real_capture.npz",window._raw_waveforms,window._frame_settings, f"真实 USB · {adapter.identity.full}")
    restored,settings,meta=load_archive(path)
    check("真实波形归档无损", all(np.array_equal(restored[n].y,w.y) and np.array_equal(restored[n].raw,w.raw) for n,w in window._raw_waveforms.items()))
    export_spreadsheet(window._raw_waveforms,str(OUT/"ds1152e_real_capture.csv"))
    app.processEvents()
    window.grab().save(str(OUT/"ds1152e_workbench.png"))
    window.scope_view.grab().save(str(OUT/"ds1152e_waveforms.png"))
    window.btn_fft.setChecked(True);app.processEvents()
    window.grab().save(str(OUT/"ds1152e_fft.png"))
    window.findChild(QTabWidget).setCurrentIndex(1);app.processEvents()
    window.grab().save(str(OUT/"ds1152e_channels.png"))
    report={"resource":resources[0],"identity":asdict(adapter.identity),"local_measurements":local,"device_measurements":device,"settings":{"channels":{n:asdict(ch) for n,ch in window._frame_settings["channels"].items()},"timebase":asdict(window._frame_settings["timebase"]),"trigger":asdict(window._frame_settings["trigger"])},"checks":checks,"gui_errors":errors}
    (OUT/"ds1152e_validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"SUCCESS {len(checks)} 真机检查通过",flush=True)
finally:
    window._disconnect()
    if baseline:
        from scopeapp.session import ScopeSession
        session=ScopeSession()
        try:
            ad=session.connect_visa(resources[0])
            ad.set_timebase(baseline["timebase"].scale)
            for name,ch in baseline["channels"].items():
                ad.set_channel_scale(ch.number,ch.scale);ad.set_channel_offset(ch.number,ch.offset);ad.set_channel_coupling(ch.number,ch.coupling.value)
            tr=baseline["trigger"]
            ad.set_trigger_source(tr.source);ad.set_trigger_level(tr.level);ad.set_trigger_slope(tr.slope);ad.set_trigger_mode(tr.mode);ad.run()
            ad.read_timebase()
        finally: session.disconnect()
    window.close()
