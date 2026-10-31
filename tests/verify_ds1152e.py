"""DS1152E 适配开发验证：波形换算、RAW 模式、写命令。"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\CODE\scope")

import numpy as np

from scopeapp.transport import VisaTransport
from scopeapp.scpi import ScpiEngine, parse_ieee_block

RES = "USB0::0x1AB1::0x0588::DS1EI182307208::INSTR"


def ask(eng, cmd, timeout=5.0):
    eng.transport.timeout = int(timeout * 1000)
    t0 = time.perf_counter()
    try:
        r = eng.ask(cmd)
        return r, (time.perf_counter() - t0) * 1000
    except Exception as e:
        return None, (time.perf_counter() - t0) * 1000


def ask_bytes(eng, cmd, timeout=30.0):
    eng.transport.timeout = int(timeout * 1000)
    t0 = time.perf_counter()
    try:
        r = eng.ask_binary(cmd)
        p, _ = parse_ieee_block(r)
        return p, (time.perf_counter() - t0) * 1000
    except Exception as e:
        return None, (time.perf_counter() - t0) * 1000


def volts(codes, scale, offset):
    """DS1152E 校准方波实测：码值向下增大，125 为网格中心。"""
    return (125.0 - codes.astype(float)) * scale / 25.0 - offset


def main() -> None:
    tr = VisaTransport(RES, timeout=4000)
    eng = ScpiEngine(tr)
    ident = eng.connect()
    print(f"设备: {ident.full}\n")

    # 写命令验证（写后读回）
    print("== 写命令验证 ==")
    for w, q in [(":CHAN1:SCAL 1", ":CHAN1:SCAL?"),
                 (":CHAN1:SCAL 2", ":CHAN1:SCAL?"),
                 (":CHAN1:OFFS 0", ":CHAN1:OFFS?"),
                 (":CHAN1:OFFS 2", ":CHAN1:OFFS?"),
                 (":TIM:SCAL 1e-3", ":TIM:SCAL?"),
                 (":TIM:SCAL 2e-3", ":TIM:SCAL?"),
                 (":TRIG:EDGE:LEV 0.1", ":TRIG:EDGE:LEV?"),
                 (":TRIG:EDGE:LEV 0", ":TRIG:EDGE:LEV?")]:
        eng.transport.timeout = 3000
        eng.write(w)   # 写命令不应挂起
        time.sleep(0.05)
        r, ms = ask(eng, q)
        print(f"  {w:22s} -> {q:16s} = {r!r} ({ms:.0f}ms)")

    # 波形：NORM 模式 + 公式验证
    print("\n== 波形 NORM 模式 ==")
    eng.write(":WAV:POIN:MODE NORM")
    time.sleep(0.05)
    for src in ["CHAN1", "CHAN2"]:
        p, ms = ask_bytes(eng, f":WAV:DATA? {src}")
        if p is None:
            print(f"  {src}: 读取失败")
            continue
        codes = np.frombuffer(p, dtype=np.uint8)
        scale = float((ask(eng, f":CHAN{src[-1]}:SCAL?")[0]))
        offs = float((ask(eng, f":CHAN{src[-1]}:OFFS?")[0]))
        v = volts(codes, scale, offs)
        m = float((ask(eng, f":MEAS:VPP? {src}")[0]))
        print(f"  {src}: {len(codes)} 点, 码值 {codes.min()}-{codes.max()}, "
              f"换算Vpp={np.ptp(v):.4f}V, 设备测量Vpp={m:.4f}V")

    # RAW 模式（8k 点）
    print("\n== 波形 RAW 模式 ==")
    r, ms = ask(eng, ":WAV:POIN:MODE?")
    print(f"  POIN:MODE? -> {r!r}")
    eng.write(":WAV:POIN:MODE RAW")
    time.sleep(0.1)
    p, ms = ask_bytes(eng, ":WAV:DATA? CHAN1", timeout=60)
    if p is not None:
        codes = np.frombuffer(p, dtype=np.uint8)
        scale = 2.0
        offs = 2.0
        v = volts(codes, scale, offs)
        print(f"  RAW: {len(codes)} 点, 换算Vpp={np.ptp(v):.4f}V, 耗时 {ms:.0f}ms")
    else:
        print(f"  RAW 读取失败 ({ms:.0f}ms)")
    eng.write(":WAV:POIN:MODE NORM")
    time.sleep(0.05)

    # 运行控制
    print("\n== 运行控制 ==")
    eng.transport.timeout = 3000
    eng.write(":RUN"); time.sleep(0.3)
    r, ms = ask(eng, ":ACQ:STATE?")
    print(f"  RUN 后 ACQ:STATE? -> {r!r}")
    eng.write(":STOP"); time.sleep(0.3)
    r, ms = ask(eng, ":ACQ:STATE?")
    print(f"  STOP 后 ACQ:STATE? -> {r!r}")
    eng.write(":RUN")

    eng.disconnect()


if __name__ == "__main__":
    main()
