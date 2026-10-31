"""并发压力测试：worker 轮询 + 主线程截图重建（复现截图回退问题）。"""
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\CODE\scope")

from scopeapp.session import ScopeSession
from scopeapp.transport import list_visa_resources

RES = next((r for r in list_visa_resources() if "0x1AB1" in r), None)
assert RES, "未找到 Rigol"
sess = ScopeSession()
ad = sess.connect_visa(RES)
print("适配器:", ad.name)

stop = threading.Event()
worker_fail = {"n": 0}


def worker_loop():
    while not stop.is_set():
        try:
            ad.fetch_all()
            ad.read_timebase()
        except Exception as e:
            worker_fail["n"] += 1
            print("worker 异常:", str(e)[:80])


t = threading.Thread(target=worker_loop, daemon=True)
t.start()
time.sleep(0.5)

recon_fail = 0
for i in range(15):
    try:
        data = ad.render_screen_snapshot()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            recon_fail += 1
            print(f"第{i}次: 非 PNG!")
    except Exception as e:
        recon_fail += 1
        print(f"第{i}次重建异常: {str(e)[:90]}")
    time.sleep(0.1)

stop.set()
t.join(timeout=5)
print(f"结果: worker 异常 {worker_fail['n']} 次, 重建失败 {recon_fail}/15")
sess.disconnect()
