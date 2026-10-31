"""诊断：GUI 启动是否会把设备 CH1 显示关掉。

流程：先手动开 CH1 → 启动 MainWindow 连真机跑数轮 → 检查设备 DISP 与 checkbox。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\CODE\scope")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from scopeapp.session import ScopeSession
from scopeapp.transport import list_visa_resources
from scopeapp.ui.main_window import MainWindow

RES = next((r for r in list_visa_resources() if "0x1AB1" in r), None)
assert RES, "未找到 Rigol"

# 1) 先手动把 CH1 打开
sess = ScopeSession()
ad = sess.connect_visa(RES)
ad.set_channel_enabled(1, True)
print("手动开启 CH1 后 DISP =", ad.engine.ask(":CHAN1:DISP?"))
sess.disconnect()

# 2) 启动 GUI 连真机
app = QApplication(sys.argv)
win = MainWindow()
win.show()
win._do_connect({"method": 2, "host": "", "port": 5025, "visa": RES, "adapter": None})

state = {"n": 0, "last_check": None}


def tick():
    try:
        state["n"] += 1
        disp = win.session.adapter.engine.ask(":CHAN1:DISP?")
        cb = win._ch_widgets[1]["enabled"]
        print(f"轮{state['n']}: 设备 CH1:DISP={disp}  checkbox={cb.isChecked()}")
        if state["n"] == 6:
            print("\n--- 模拟用户勾选 CH1 ---")
            cb.setChecked(True)  # 触发 toggled → set_channel_enabled(1, True)
        if state["n"] == 10:
            disp = win.session.adapter.engine.ask(":CHAN1:DISP?")
            print(f"\n最终: 设备 CH1:DISP={disp} checkbox={win._ch_widgets[1]['enabled'].isChecked()}")
            win.close()
            app.quit()
    except Exception as e:
        print("异常:", type(e).__name__, str(e)[:80])
        app.quit()


t = QTimer()
t.timeout.connect(tick)
t.start(500)
QTimer.singleShot(20000, app.quit)
app.exec()
