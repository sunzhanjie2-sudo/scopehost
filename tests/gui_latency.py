"""GUI 事件循环延迟实测：主线程是否被设备 IO 阻塞。"""
import sys
import time

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
t0 = time.perf_counter()
win._do_connect({"method": 2, "host": "", "port": 5025, "visa": RES, "adapter": None})
print(f"连接耗时: {(time.perf_counter()-t0)*1000:.0f}ms")

intervals = []
st = {"last": time.perf_counter(), "n": 0}


def tick():
    now = time.perf_counter()
    intervals.append((now - st["last"]) * 1000)
    st["last"] = now
    st["n"] += 1
    if st["n"] >= 100:  # 10 秒
        slow = [d for d in intervals if d > 200]
        print(f"100 tick(10s): 平均 {sum(intervals)/len(intervals):.0f}ms, "
              f">200ms 的 tick: {len(slow)} 次, 最大 {max(intervals):.0f}ms")
        print(f"慢 tick 明细: {[round(d) for d in slow][:10]}")
        win.close()
        app.quit()


t = QTimer()
t.timeout.connect(tick)
t.start(100)
QTimer.singleShot(20000, app.quit)
app.exec()
