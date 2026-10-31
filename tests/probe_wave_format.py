"""DS1152E 波形格式与抓屏深入诊断。"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\CODE\scope")

import numpy as np

from scopeapp.transport import VisaTransport
from scopeapp.scpi import ScpiEngine, parse_ieee_block

RES = "USB0::0x1AB1::0x0588::DS1EI182307208::INSTR"


def main() -> None:
    tr = VisaTransport(RES, timeout=4000)
    eng = ScpiEngine(tr)
    ident = eng.connect()
    print(f"设备: {ident.full}\n")

    # 1) 完整 dump :WAV:DATA?
    resp = eng.ask_binary(":WAV:DATA?")
    payload, off = parse_ieee_block(resp)
    print(f":WAV:DATA? 总 {len(resp)} 字节, 块偏移 {off}, payload {len(payload)} 字节")
    print(f"payload 前 20 字节: {payload[:20].hex(' ')}")
    print(f"payload 后 10 字节: {payload[-10:].hex(' ')}")
    arr = np.frombuffer(payload, dtype=np.uint8)
    print(f"数值分布: min={arr.min()} max={arr.max()} unique={np.unique(arr).size}")
    cc = (arr == 0xCC).mean()
    print(f"0xCC(无效点)占比: {cc:.1%}")
    # 有信号段
    sig = arr[arr != 0xCC]
    if sig.size:
        print(f"有效段: {sig.size} 点, min={sig.min()} max={sig.max()} mean={sig.mean():.1f}")

    # 2) :DISPlay:DATA? 长超时重试（老机器可能生成慢）
    print("\n:DISPlay:DATA? 用 60s 超时重试...")
    tr.timeout = 60000
    t0 = time.perf_counter()
    try:
        resp2 = eng.ask_binary(":DISPlay:DATA?")
        dt = time.perf_counter() - t0
        p2, off2 = parse_ieee_block(resp2)
        print(f"成功! {len(resp2)} 字节, payload {len(p2)} 字节, 头 {p2[:2]!r}, 耗时 {dt:.1f}s")
    except Exception as e:
        dt = time.perf_counter() - t0
        print(f"仍失败: {e} ({dt:.1f}s)")
    tr.timeout = 4000

    # 3) 写命令测试
    print("\n写命令测试:")
    for c in [":CHAN1:SCAL 1", ":CHAN1:OFFS 0", ":TIM:SCAL 1e-3",
              ":TRIG:EDGE:LEV 0", ":TRIG:EDGE:SOUR CH1", ":RUN", ":STOP"]:
        t0 = time.perf_counter()
        try:
            eng.write(c)
            eng.transport.timeout = 2000
            err = eng.ask(":SYST:ERR?")
            dt = time.perf_counter() - t0
            print(f"  {c:24s} -> err={err!r} ({dt*1000:.0f}ms)")
        except Exception as e:
            print(f"  {c:24s} -> FAIL: {str(e)[:60]}")
        tr.timeout = 4000

    eng.disconnect()


if __name__ == "__main__":
    main()
