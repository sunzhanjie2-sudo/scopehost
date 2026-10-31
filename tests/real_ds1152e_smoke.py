"""DS1152E 真机端到端冒烟：适配器识别、波形采集、抓屏重建。"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\CODE\scope")

import numpy as np

from scopeapp.adapters import detect_adapter, RigolDs1000eAdapter
from scopeapp.session import ScopeSession
from scopeapp.transport import list_visa_resources

RES = None
for r in list_visa_resources():
    if "0x1AB1" in r:
        RES = r
        break
if RES is None:
    print("未找到 Rigol USB 设备"); sys.exit(1)
print(f"设备资源: {RES}\n")

sess = ScopeSession()
ad = sess.connect_visa(RES)
print(f"识别适配器: {ad.name}  (期望 Rigol DS1000E/D)")
assert isinstance(ad, RigolDs1000eAdapter), "适配器选择错误!"

ident = ad.identity
print(f"设备: {ident.full}\n")

# 1) 波形采集（含设置）
t0 = time.perf_counter()
wf = ad.fetch_all()
dt = (time.perf_counter() - t0) * 1000
for name, w in wf.items():
    print(f"  {name}: {w.points} 点, Vpp={w.vpp:.4f}V, Vavg={w.vmean:.4f}V, "
          f"窗口={w.duration*1e3:.2f}ms, x_incr={w.x_incr:.3g}s")
print(f"  采集耗时: {dt:.0f}ms\n")

# 2) 测量对比（设备自带测量）
ch1 = ad.read_channel(1)
m = ad.engine.ask(":MEAS:VPP? CHAN1")
print(f"设备 :MEAS:VPP? CHAN1 = {m}V\n")

# 3) 连续采集 5 轮（模拟 GUI 轮询）
t0 = time.perf_counter()
for _ in range(5):
    wf = ad.fetch_all()
dt = (time.perf_counter() - t0) * 1000 / 5
print(f"连续采集平均: {dt:.0f}ms/轮\n")

# 4) 抓屏：不支持远程 → 数据重建
try:
    ad.capture_screen()
    print("capture_screen: 意外成功")
except NotImplementedError as e:
    print(f"capture_screen: 按预期不支持 ({str(e)[:30]}...)")
img_bytes = ad.render_screen_snapshot()
open("device_render.png", "wb").write(img_bytes)
print(f"render_screen_snapshot: {len(img_bytes)} 字节 → device_render.png")

from PIL import Image
import io
im = Image.open(io.BytesIO(img_bytes))
px = np.array(im.convert("RGB"))
dark = (px.sum(axis=2) < 150).mean()
yellow = ((abs(px[..., 0].astype(int) - 255) < 40)
          & (abs(px[..., 1].astype(int) - 213) < 40)).sum()
print(f"重建图: {im.size}, 暗背景 {dark:.1%}, 黄色波形像素 {yellow}")

sess.disconnect()
print("\n冒烟通过")
