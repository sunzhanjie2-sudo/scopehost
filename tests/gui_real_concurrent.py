"""GUI 真机并发截图验证：worker 轮询中反复截图。"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\CODE\scope")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from scopeapp.transport import list_visa_resources
from scopeapp.ui.main_window import MainWindow

RES = next((r for r in list_visa_resources() if "0x1AB1" in r), None)
assert RES, "未找到 Rigol"

app = QApplication(sys.argv)
win = MainWindow()
win.show()
win._do_connect({"method": 2, "host": "", "port": 5025, "visa": RES, "adapter": None})

state = {"n": 0}


def tick():
    try:
        state["n"] += 1
        if state["n"] in (8, 14):
            ad = win.session.adapter
            try:
                data = ad.capture_screen()
                src = "真机抓屏"
            except NotImplementedError:
                data = ad.render_screen_snapshot()
                src = "数据重建"
            ok = data[:8] == b"\x89PNG\r\n\x1a\n"
            print(f"截图#{state['n']}: 来源={src}, PNG={ok}, {len(data)}字节")
        if state["n"] >= 16:
            print("GUI真机并发截图 OK, 波形通道:", list(win.scope_view.waveforms.keys()))
            win.close()
            app.quit()
    except Exception as e:
        print("异常:", type(e).__name__, str(e)[:80])
        app.quit()


t = QTimer()
t.timeout.connect(tick)
t.start(400)
QTimer.singleShot(20000, app.quit)
app.exec()
