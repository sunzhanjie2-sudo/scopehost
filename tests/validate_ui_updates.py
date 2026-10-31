"""DS1152E 真机验收：输入提交、加减、带宽读回、双向光标和完整窗口保存。"""
from pathlib import Path
import json
import os
import sys
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "artifacts"
OUT.mkdir(exist_ok=True)
os.environ["SCOPEHOST_LOG"] = str(OUT / "ds1152e_ui_v0.4_scpi.log")
import numpy as np
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTabWidget
from scopeapp.session import ScopeSession
from scopeapp.ui.main_window import MainWindow
from scopeapp.transport import list_visa_resources

resources = [r for r in list_visa_resources() if "DS1EI182307208" in r]
assert len(resources) == 1, f"未找到 DS1152E：{resources}"
resource = resources[0]
checks, slot_errors, before, after = [], [], {}, {}
def report_exception(kind, value, trace):
    slot_errors.append(str(value))
    sys.__excepthook__(kind, value, trace)

sys.excepthook = report_exception
app = QApplication([])
w = None

def check(name, condition):
    assert condition, name
    checks.append(name)
    print("PASS", name, flush=True)

def until(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if slot_errors:
            raise AssertionError(slot_errors)
        if predicate():
            return
        QTest.qWait(20)
    raise AssertionError("等待设备超时：" + (w.statusBar().currentMessage() if w else ""))

queries = [":CHAN1:OFFS?", ":CHAN2:OFFS?", ":CHAN1:BWL?", ":CHAN2:BWL?", ":TRIG:STAT?", ":WAV:POIN:MODE?"]
initial = ScopeSession()
try:
    initial.connect_visa(resource)
    before = {q: initial.engine.ask(q).strip() for q in queries}
    initial.engine.write(":CHAN1:BWL ON")
    time.sleep(.3)
    check("DS1152E 读回 CH1 20 MHz 限制", initial.engine.ask(":CHAN1:BWL?").strip().upper() in {"1", "ON"})
    initial.disconnect()
    w = MainWindow(visa_resource=resource)
    w.show()
    until(lambda: w._frames >= 1)
    enabled = {name for name, ch in w._settings["channels"].items() if ch.enabled}
    check("真实 USB 按实际启用通道读取，无模拟器", w._sim is None and "CH1" in enabled and set(w._raw_waveforms) == enabled)
    check("波形下方显示实际 20 MHz", "CH1 20 MHz" in w.lbl_bandwidth.text())
    tabs = w.findChild(QTabWidget)
    tabs.setCurrentIndex(1)
    app.processEvents()
    for n in (1, 2):
        control = w._ch_widgets[n]["offset"]
        scale = w._settings["channels"][f"CH{n}"].scale
        target = scale / 2
        control.setFocus()
        control.lineEdit().selectAll()
        QTest.keyClicks(control, f"{target:.6f}")
        # 等待一轮设置刷新，让测试覆盖正在编辑的真实输入。
        for _ in range(120):
            app.processEvents()
            QTest.qWait(20)
        check(f"CH{n} 输入未被轮询覆盖", abs(float(control.text()) - target) < 1e-6)
        QTest.keyClick(control, Qt.Key_Return)
        until(lambda: abs(w._settings["channels"][f"CH{n}"].offset - target) < 1e-6 and not w._pending_numeric)
        check(f"CH{n} 数字偏移真实写入并读回", abs(float(w.session.engine.ask(f":CHAN{n}:OFFS?")) - target) < 1e-6)
        step = control.singleStep()
        QTest.mouseClick(control.minus_button, Qt.LeftButton)
        until(lambda: abs(w._settings["channels"][f"CH{n}"].offset - (target - step)) < 1e-6 and not w._pending_numeric)
        check(f"CH{n} 左侧减号生效", abs(control.value() - (target - step)) < 1e-6)
        QTest.mouseClick(control.plus_button, Qt.LeftButton)
        until(lambda: abs(w._settings["channels"][f"CH{n}"].offset - target) < 1e-6 and not w._pending_numeric)
        check(f"CH{n} 右侧加号生效", abs(control.value() - target) < 1e-6)
        control.setValue(float(before[f":CHAN{n}:OFFS?"]))
        until(lambda: abs(w._settings["channels"][f"CH{n}"].offset - float(before[f":CHAN{n}:OFFS?"])) < 1e-6 and not w._pending_numeric)
    w.btn_cursor.click()
    w.cmb_cursor_scope.setCurrentIndex(1)
    values = w.scope_view.cursor_values()
    for name, readings in values["channels"].items():
        wf = w.scope_view.waveforms[name]
        expected = np.interp(w.scope_view.cursor_times, wf.x, wf.y)
        check(f"{name} 垂直光标幅值匹配真实采样", np.allclose([readings["va"], readings["vb"]], expected))
    check("全部启用通道电压读数可见", all(f"{name}：A" in w.lbl_cursor.text() for name in enabled))
    w.btn_stacked.click()
    w.btn_zoom.click()
    w.cmb_cursor_mode.setCurrentIndex(2)
    app.processEvents()
    view = w.zoom_view
    ch = view.channels["CH1"]
    x0, top, width, height = view._lane_geometry("CH1")
    x, y = int(x0 + width * .7), int(view._volts_to_px(view.cursor_voltages[0], ch, "CH1"))
    target_y = int(top + height * .65)
    QTest.mousePress(view, Qt.LeftButton, pos=QPoint(x, y))
    QTest.mouseMove(view, QPoint(x, target_y))
    QTest.mouseRelease(view, Qt.LeftButton, pos=QPoint(x, target_y))
    check("堆叠 ZOOM 水平光标电压正确", abs(view.cursor_voltages[0] - view._px_to_volts(target_y, ch, "CH1")) < 1e-6)
    check("总览和 ZOOM 光标同步", view.cursor_voltages == w.scope_view.cursor_voltages)
    w.cmb_cursor_mode.setCurrentIndex(0)
    app.processEvents()
    path = OUT / "ds1152e_ui_v0.4.png"
    with patch.object(w, "_path", return_value=str(path)):
        w._save_plot()
    check("保存图片包含完整软件窗口", QImage(str(path)).size() == w.grab().size())
finally:
    initial.disconnect()
    if w:
        w.close()
        app.processEvents()
    if before:
        restore = ScopeSession()
        try:
            ad = restore.connect_visa(resource)
            for n in (1, 2):
                ad.set_channel_offset(n, float(before[f":CHAN{n}:OFFS?"]))
                mode = "ON" if before[f":CHAN{n}:BWL?"].upper() in {"1", "ON"} else "OFF"
                ad._write(f":CHAN{n}:BWL {mode}", settle=True)
            ad._wait_settled()
            restore.engine.write(f":WAV:POIN:MODE {before[':WAV:POIN:MODE?']}")
            after = {q: restore.engine.ask(q).strip() for q in queries}
        finally:
            restore.disconnect()
        check("测试后恢复两通道偏移及带宽限制", all(before[q] == after[q] for q in queries if q != ":TRIG:STAT?"))
        check("保持原有运行或停止状态", (before[":TRIG:STAT?"] == "STOP") == (after[":TRIG:STAT?"] == "STOP"))
        (OUT / "ds1152e_ui_v0.4_validation.json").write_text(json.dumps({"resource": resource, "before": before,
            "after": after, "checks": checks, "slot_errors": slot_errors}, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"真实设备验收：{len(checks)} 项通过", flush=True)
