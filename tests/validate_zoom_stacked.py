"""DS1152E 真机 ZOOM/堆叠验收，只改变本地显示，保持设备原有运行状态。"""
from pathlib import Path
import os
import sys
import time
import json
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
OUT=ROOT/"artifacts"
os.environ["SCOPEHOST_LOG"]=str(OUT/"ds1152e_zoom_scpi.log")
import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt,QPoint,QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from scopeapp.ui.main_window import MainWindow
from scopeapp.transport import list_visa_resources
from scopeapp.measurements import measure
from scopeapp.export import export_spreadsheet

APP=QApplication([])
errors=[]
sys.excepthook=lambda kind,value,trace:errors.append(str(value))
resources=[r for r in list_visa_resources() if "0x1AB1" in r and "0x0588" in r]
assert len(resources)==1, f"未发现唯一 DS1152E: {resources}"
w=MainWindow(visa_resource=resources[0]);w.show()
def until(fn,timeout=12):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        APP.processEvents()
        if errors: raise AssertionError(errors)
        if fn(): return
        time.sleep(.02)
    raise AssertionError(w.lbl_status.text()+" "+w.statusBar().currentMessage())

def settings():
    return {q:w.session.engine.ask(q).strip() for q in [":TIM:SCAL?",":TIM:OFFS?",":CHAN1:SCAL?",":CHAN1:OFFS?",":CHAN2:SCAL?",":CHAN2:OFFS?",":TRIG:STAT?"]}

try:
    until(lambda:w._frames>=1)
    assert w._sim is None and w.session.adapter.identity.model=="DS1152E"
    assert set(w._raw_waveforms)=={"CH1","CH2"}
    if w._state=="RUN": until(lambda:w._frames>=3)
    before=settings()
    writes_before=[line for line in (OUT/"ds1152e_zoom_scpi.log").read_text(encoding="utf-8").splitlines() if "] W " in line]
    old_wave=w._raw_waveforms["CH1"];old_samples=old_wave.y.copy()
    w.btn_stacked.click();APP.processEvents()
    assert w.scope_view._lane_geometry("CH1")[1]+w.scope_view._lane_geometry("CH1")[3] <= w.scope_view._lane_geometry("CH2")[1]+1e-9
    w.grab().save(str(OUT/"ds1152e_stacked.png"))
    w.btn_zoom.click();APP.processEvents()
    assert np.isclose(w.zoom_view.timebase.scale,w.scope_view.timebase.scale/4)
    assert w.zoom_view.stacked and w.zoom_view._lane_names()==["CH1","CH2"]
    w.grab().save(str(OUT/"ds1152e_zoom_stacked.png"))
    event=QWheelEvent(QPointF(400,80),QPointF(400,80),QPoint(),QPoint(0,120),Qt.NoButton,Qt.NoModifier,Qt.ScrollUpdate,False)
    APP.sendEvent(w.zoom_view,event)
    assert w.spn_zoom_factor.value()==8
    QTest.mousePress(w.zoom_view,Qt.LeftButton,pos=QPoint(400,90));QTest.mouseMove(w.zoom_view,QPoint(430,90));QTest.mouseRelease(w.zoom_view,Qt.LeftButton,pos=QPoint(430,90))
    assert w.spn_zoom_center.value()!=0
    w._reset_zoom();w.btn_cursor.click();APP.processEvents()
    assert w.scope_view.cursor_times==w.zoom_view.cursor_times
    assert "dv" in w.zoom_view.cursor_values()
    w.btn_reference.click();assert set(w.zoom_view.references)=={"CH1","CH2"}
    w.btn_fft.click();APP.processEvents();w.grab().save(str(OUT/"ds1152e_zoom_stacked_fft.png"))
    cropped=w._zoom_waveforms()
    start,end=w.scope_view.zoom_range
    for n,wf in cropped.items():
        source=w.scope_view.waveforms[n]
        mask=(source.x>=start)&(source.x<=end)
        np.testing.assert_array_equal(wf.y,source.y[mask])
    export_spreadsheet(cropped,str(OUT/"ds1152e_zoom.csv"))
    after=settings()
    for q in before:
        if q!=":TRIG:STAT?":assert before[q]==after[q], (q,before[q],after[q])
    assert (before[":TRIG:STAT?"]=="STOP")== (after[":TRIG:STAT?"]=="STOP")
    writes_after=[line for line in (OUT/"ds1152e_zoom_scpi.log").read_text(encoding="utf-8").splitlines() if "] W " in line]
    assert writes_before==writes_after, "显示操作发出了硬件写命令"
    np.testing.assert_array_equal(old_wave.y,old_samples)
    report={"identity":w.session.adapter.identity.full,"source":w._source,"before":before,"after":after,
            "zoom_range_s":list(w.scope_view.zoom_range),"zoom_points":{n:v.points for n,v in cropped.items()},
            "measurements":{n:{m.name:m.value for m in measure(v)} for n,v in w._raw_waveforms.items()},
            "checks":["真实 DS1152E CH1/CH2","独立堆叠区域","总览/局部倍率一致","局部滚轮放大","局部拖动平移","游标同步","参考波形同步","FFT 同时显示","导出实际区间采样","原始样本未修改","硬件设置和运行状态未修改","无新增硬件写命令"],"gui_errors":errors}
    (OUT/"ds1152e_zoom_validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
finally:w.close()
