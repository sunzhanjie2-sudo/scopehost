"""长时间监控：worker 轮询耗时（找卡顿）+ CH1 DISP 变化时刻（找关闭源）。"""
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
print("适配器:", ad.name, "| CH1:DISP =", ad.engine.ask(":CHAN1:DISP?"))

stop = threading.Event()
slow_rounds = []
disp_history = []


def worker_loop():
    """模拟 GUI 的 worker：每 0.4s fetch_all，每 2s 刷新设置。"""
    last_settings = 0.0
    while not stop.is_set():
        t0 = time.perf_counter()
        try:
            ad.fetch_all()
            if time.perf_counter() - last_settings >= 2.0:
                for n in range(1, ad.channel_count + 1):
                    ad.read_channel(n)
                ad.read_timebase()
                ad.read_trigger()
                last_settings = time.perf_counter()
        except Exception as e:
            slow_rounds.append((time.perf_counter() - t0, f"异常: {str(e)[:50]}"))
        dt = time.perf_counter() - t0
        if dt > 0.5:
            slow_rounds.append((dt, "慢轮"))
        time.sleep(max(0.05, 0.4 - dt))


def monitor_loop():
    last_disp = None
    while not stop.is_set():
        try:
            d = ad.engine.ask(":CHAN1:DISP?")
            if last_disp is not None and d != last_disp:
                disp_history.append((time.monotonic(), last_disp, d))
                print(f"[{time.monotonic():.0f}] CH1:DISP {last_disp} -> {d}  !!!")
            last_disp = d
        except Exception as e:
            print("监控异常:", str(e)[:60])
        time.sleep(0.5)


t1 = threading.Thread(target=worker_loop, daemon=True)
t2 = threading.Thread(target=monitor_loop, daemon=True)
t1.start()
t2.start()
time.sleep(60)
stop.set()
t1.join(timeout=5)
t2.join(timeout=5)

print(f"\n=== 60s 监控结果 ===")
print(f"慢轮/异常次数: {len(slow_rounds)}")
for dt, tag in slow_rounds[:10]:
    print(f"  {dt:.2f}s  {tag}")
print(f"CH1 DISP 变化: {len(disp_history)} 次", disp_history[:5] if disp_history else "")
print(f"最终 CH1:DISP =", ad.engine.ask(":CHAN1:DISP?"))
sess.disconnect()
