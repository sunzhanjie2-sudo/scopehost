"""DS1152E 真机命令探测脚本。"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\CODE\scope")

from scopeapp.transport import VisaTransport
from scopeapp.scpi import ScpiEngine, parse_ieee_block

RES = "USB0::0x1AB1::0x0588::DS1EI182307208::INSTR"


def probe(eng: ScpiEngine, cmds: list[str], timeout: float = 3.0) -> None:
    for c in cmds:
        eng.transport.timeout = int(timeout * 1000)
        t0 = time.perf_counter()
        try:
            r = eng.ask(c)
            dt = time.perf_counter() - t0
            print(f"  {c:28s} -> {r[:80]!r}  ({dt*1000:.0f}ms)")
        except Exception as e:
            dt = time.perf_counter() - t0
            msg = str(e).split(":")[-1].strip()[:40]
            print(f"  {c:28s} -> FAIL({msg}) ({dt*1000:.0f}ms)")


def main() -> None:
    tr = VisaTransport(RES, timeout=4000)
    eng = ScpiEngine(tr)
    ident = eng.connect()
    print(f"设备: {ident.full}\n")

    print("== 波形相关 ==")
    probe(eng, [":WAV:SOUR?", ":WAV:FORM?", ":WAV:MODE?", ":WAV:DATA?",
                ":WAV:STAR?", ":WAV:STOP?", ":WAV:POIN?"], timeout=4)

    print("\n== 抓屏 ==")
    # 直接 ask_binary 拿块，给 30s（老机器生成 BMP 慢）
    tr.timeout = 30000
    try:
        t0 = time.perf_counter()
        resp = eng.ask_binary(":DISPlay:DATA?")
        dt = time.perf_counter() - t0
        payload, off = parse_ieee_block(resp)
        print(f"  :DISPlay:DATA? -> {len(resp)} 字节, payload {len(payload)} 字节, 头 {resp[:6]!r} ({dt*1000:.0f}ms)")
        print(f"  文件头: {payload[:2]!r} (BM=BMP)")
    except Exception as e:
        print(f"  :DISPlay:DATA? -> FAIL: {e}")
    tr.timeout = 4000

    print("\n== 触发 ==")
    probe(eng, [":TRIG:EDGE:SOUR?", ":TRIG:EDGE:LEV?", ":TRIG:LEV?", ":TRIG:MODE?", ":TRIG:SWE?"], timeout=3)

    print("\n== 测量 ==")
    probe(eng, [":MEAS:VPP?", ":MEAS:VPP? CHAN1", ":MEAS:ALL?", ":MEAS:ITEM? VPP,CHAN1"], timeout=5)

    print("\n== 采集/时基/通道 ==")
    probe(eng, [":ACQ:MODE?", ":ACQ:SAMP?", ":ACQ:MDEP?", ":TIM:SCAL?", ":TIM:OFF?",
                ":CHAN1:SCAL?", ":CHAN1:OFFS?", ":CHAN1:COUP?", ":CHAN1:DISP?"], timeout=3)

    print("\n== 保存/文件 ==")
    probe(eng, [":SAVE:IMAG?", ":SAVE:WAV?", ":SAVE:WAV:FILE?", ":REC:STATE?"], timeout=4)

    eng.disconnect()


if __name__ == "__main__":
    main()
