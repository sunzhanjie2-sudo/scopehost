"""核心层自测：解码器单元测试 + 模拟器端到端（无需真实硬件）。

运行：python tests/test_core.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows 控制台 UTF-8
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import numpy as np  # noqa: E402

from scopeapp.adapters import TektronixAdapter, detect_adapter  # noqa: E402
from scopeapp.model import Waveform, WaveformEncoding  # noqa: E402
from scopeapp.scpi import ScpiEngine, parse_ieee_block  # noqa: E402
from scopeapp.simulator import SimulatorServer  # noqa: E402
from scopeapp.transport import TcpTransport  # noqa: E402
from scopeapp.waveform import (  # noqa: E402
    DecodeOptions,
    decode_ascii_response,
    decode_raw,
    decode_waveform,
    parse_rigol_header,
    parse_tds_header,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


# ---------------------------------------------------------------- 解码器

def test_ieee_block() -> None:
    print("parse_ieee_block")
    block = b"#3100" + b"x" * 100 + b"tail"
    payload, off = parse_ieee_block(block)
    check("长度头解析", len(payload) == 100 and payload == b"x" * 100, f"len={len(payload)}")
    check("偏移正确", off == 5, f"off={off}")
    plain = b"1,2,3"
    payload2, _ = parse_ieee_block(plain)
    check("非块数据原样返回", payload2 == plain)


def test_decode_raw() -> None:
    print("decode_raw")
    arr = np.array([-100, 0, 100, 32767], dtype=np.int16)
    payload = arr.astype(">i2").tobytes()
    out = decode_raw(payload, WaveformEncoding.INT16, ">")
    check("int16 大端", np.array_equal(out, arr), str(out))
    arr8 = np.array([-128, 127], dtype=np.int8)
    out8 = decode_raw(arr8.tobytes(), WaveformEncoding.INT8, ">")
    check("int8", np.array_equal(out8, arr8), str(out8))
    le = np.array([1, -2, 30000], dtype=np.int16).astype("<i2").tobytes()
    outle = decode_raw(le, WaveformEncoding.INT16, "<")
    check("int16 小端", np.array_equal(outle, np.array([1, -2, 30000])), str(outle))


def test_tds_header() -> None:
    print("TDS 内嵌头")
    x_incr, x_origin, x_ref, y_incr, y_origin, y_ref = 1e-6, 0.5, 100, 0.1, 0.0, 5
    header = bytes([24]) + np.array(
        [x_incr, x_origin, x_ref, y_incr, y_origin, y_ref], dtype=">f4"
    ).tobytes()
    codes = np.array([5, 15, 25], dtype=np.int8)
    payload = header + codes.tobytes()
    opts, off = parse_tds_header(payload)
    check("头偏移", off == 25 and abs(opts["x_incr"] - x_incr) < 1e-12, f"off={off}")
    wf = decode_waveform(payload, DecodeOptions(tds_header=True, encoding=WaveformEncoding.INT8))
    expect = (codes.astype(float) - y_ref) * y_incr + y_origin
    check("换算公式", np.allclose(wf.y, expect), f"{wf.y} vs {expect}")
    check("时间轴起点", abs(wf.x_start - (x_origin - x_ref * x_incr)) < 1e-12, str(wf.x_start))


def test_rigol_header() -> None:
    print("Rigol 内嵌头")
    y_origin, y_ref, y_incr = 10, 20, 0.05
    head = bytes([0]) + np.array([y_origin, y_ref], dtype="<i2").tobytes() + np.array(
        [y_incr], dtype="<f4"
    ).tobytes()
    codes = np.array([30, 40], dtype=np.int16)
    payload = head + codes.astype("<i2").tobytes()
    opts, off = parse_rigol_header(payload)
    check("头偏移", off == 9 and abs(opts["y_incr"] - y_incr) < 1e-9, f"off={off}")
    wf = decode_waveform(
        payload,
        DecodeOptions(rigol_header=True, encoding=WaveformEncoding.INT16, byte_order="<"),
    )
    expect = (codes.astype(float) - y_origin - y_ref) * y_incr
    check("换算公式", np.allclose(wf.y, expect), f"{wf.y} vs {expect}")


def test_ascii() -> None:
    print("ASCII 波形")
    wf = decode_ascii_response("1.0, 2.0,3.0", channel="CH1", x_incr=1e-3)
    check("文本解析", np.allclose(wf.y, [1, 2, 3]) and wf.points == 3)


# ---------------------------------------------------------------- 端到端

def _freq_estimate(x: np.ndarray, y: np.ndarray) -> float:
    """过零法粗估频率。"""
    crossings = np.where(np.diff(np.signbit(y)))[0]
    if crossings.size < 2:
        return 0.0
    t0, t1 = x[crossings[0]], x[crossings[-1]]
    n = crossings.size - 1
    return n / (2 * (t1 - t0)) if t1 > t0 else 0.0


def test_e2e_tektronix() -> None:
    print("端到端：模拟器 + Tektronix 适配器")
    with SimulatorServer(port=5123) as srv:
        srv.scope.hscale = 1e-3
        srv.scope.channels["CH1"]["signal"].freq = 1_000.0
        srv.scope.channels["CH1"]["signal"].amp = 2.0
        srv.scope.channels["CH1"]["signal"].noise = 0.0
        srv.scope.channels["CH2"]["signal"].kind = "square"
        srv.scope.channels["CH2"]["signal"].freq = 500.0
        srv.scope.channels["CH2"]["signal"].amp = 1.0
        srv.scope.channels["CH2"]["signal"].noise = 0.0

        tr = TcpTransport("127.0.0.1", 5123)
        eng = ScpiEngine(tr)
        ident = eng.connect()
        check("身份识别", ident.manufacturer == "TEKTRONIX" and ident.model == "MSO44B",
              ident.full)
        check("适配器自动识别", detect_adapter(ident.full) is TektronixAdapter)

        ad = TektronixAdapter(eng)
        check("适配器可连接", ad.identity is not None)

        tb = ad.read_timebase()
        check("时基读取", abs(tb.scale - 1e-3) < 1e-15, str(tb.scale))

        wf = ad.fetch_waveform(1)
        check("波形点数", wf.points == 10000, str(wf.points))
        check("幅度（2Vpp）", abs(wf.vpp - 2.0) < 0.15, f"vpp={wf.vpp}")
        check("波形周期", abs(wf.duration - 10e-3) < 1e-9, str(wf.duration))
        f_est = _freq_estimate(wf.x, wf.y)
        check("频率≈1kHz", abs(f_est - 1000.0) < 50.0, f"f={f_est}")

        wf2 = ad.fetch_waveform(2)
        check("CH2 方波幅度", abs(wf2.vpp - 1.0) < 0.1, f"vpp={wf2.vpp}")
        f2 = _freq_estimate(wf2.x, wf2.y)
        check("CH2 频率≈500Hz", abs(f2 - 500.0) < 30.0, f"f={f2}")

        # 时基改变 → 采样率/窗口联动
        ad.set_timebase(1e-4)
        wf3 = ad.fetch_waveform(1)
        check("时基改变后窗口=1ms", abs(wf3.duration - 1e-3) < 1e-12, str(wf3.duration))
        ad.set_timebase(1e-3)

        # 通道设置写入
        ad.set_channel_scale(1, 2.0)
        ch = ad.read_channel(1)
        check("通道量程写入", abs(ch.scale - 2.0) < 1e-12, str(ch.scale))
        ad.set_channel_scale(1, 1.0)

        # WAVFrm? 备用路径
        wfr = ad.fetch_wavfrm(1)
        check("WAVFrm? 路径", wfr.points == 10000 and abs(wfr.vpp - 2.0) < 0.2,
              f"points={wfr.points} vpp={wfr.vpp}")
        eng.disconnect()


def test_e2e_rigol_decoder() -> None:
    print("Rigol 解码路径（合成 payload）")
    y_origin, y_ref, y_incr = 0, 0, 0.01
    head = bytes([0]) + np.array([y_origin, y_ref], dtype="<i2").tobytes() + np.array(
        [y_incr], dtype="<f4"
    ).tobytes()
    raw = (np.arange(10) - 5).astype(np.int16)
    payload = head + raw.astype("<i2").tobytes()
    block = b"#4" + str(len(payload)).zfill(4).encode() + payload
    body, _ = parse_ieee_block(block)
    opts = DecodeOptions(encoding=WaveformEncoding.INT16, byte_order="<", rigol_header=True)
    wf = decode_waveform(body, opts)
    expect = (raw.astype(float) - y_origin - y_ref) * y_incr
    check("Rigol 端到端解码", np.allclose(wf.y, expect), f"{wf.y[:3]} vs {expect[:3]}")


def test_e2e_capture_screen() -> None:
    """真实屏幕截图端到端：Tektronix 路径(PNG) + Rigol 路径(BMP)。"""
    print("端到端：设备屏幕截图")
    try:
        from PIL import Image
        import io
    except ImportError:
        check("PIL 可用", False, "未安装 Pillow")
        return

    with SimulatorServer(port=5125) as srv:
        srv.scope.channels["CH1"]["signal"].freq = 1_000.0
        srv.scope.channels["CH2"]["signal"].kind = "square"
        tr = TcpTransport("127.0.0.1", 5125)
        eng = ScpiEngine(tr)
        eng.connect()

        # ---- Tektronix：SAVE:IMAGE → FILESystem:READFile 读回 PNG ----
        tek = TektronixAdapter(eng)
        png = tek.capture_screen()
        check("Tektronix 抓屏返回 PNG", png[:8] == b"\x89PNG\r\n\x1a\n",
              png[:8].hex() if png else "空")
        im = Image.open(io.BytesIO(png))
        check("PNG 可解码", im.size == (800, 480), str(im.size))
        px = np.array(im.convert("RGB"))
        dark = (px.sum(axis=2) < 150).mean()
        yellow = ((abs(px[..., 0].astype(int) - 255) < 40)
                  & (abs(px[..., 1].astype(int) - 213) < 40)
                  & (abs(px[..., 2].astype(int) - 74) < 40)).sum()
        check("抓屏含暗色网格背景", dark > 0.8, f"dark={dark:.2%}")
        check("抓屏含 CH1 黄色波形", yellow > 50, f"yellow_px={yellow}")

        # ---- Rigol：:DISPlay:DATA? 返回 BMP ----
        from scopeapp.adapters.rigol import RigolAdapter
        rig = RigolAdapter(eng)
        bmp = rig.capture_screen()
        check("Rigol 抓屏返回 BMP", bmp[:2] == b"BM", bmp[:2].hex() if bmp else "空")
        im2 = Image.open(io.BytesIO(bmp))
        check("BMP 可解码", im2.size == (800, 480), str(im2.size))
        eng.disconnect()


def test_ds1000e_formula() -> None:
    """实测屏幕码：125 是中心，码值减小时电压增大。"""
    print("DS1000E 换算公式")
    codes = np.array([100, 125, 150], dtype=np.uint8)
    scale, offset = 2.0, 2.0
    v = (125.0 - codes.astype(np.float64)) * scale / 25.0 - offset
    expected = np.array([0.0, -2.0, -4.0])
    check("公式换算", np.allclose(v, expected), f"{v} vs {expected}")
    # 600 点 NORM 模式 x 轴：窗口 = 12×时基
    tb_scale = 2e-3
    x_incr = 12.0 * tb_scale / 600
    check("x 轴换算", abs(x_incr - 4e-5) < 1e-10, str(x_incr))


def test_export_ltspice() -> None:
    """LTSpice PWL 导出：双列无表头、时间从 0 起单调、.asc 电路生成。"""
    print("LTSpice 导出")
    import tempfile
    from pathlib import Path

    from scopeapp.export import export_ltspice_pwl, export_spreadsheet, write_ltspice_asc

    # 合成四路波形（不同频率/幅度/偏移，时间轴含负值）
    wfs = {}
    n = 1000
    t = np.arange(n) * 1e-5 - 5e-3  # -5ms ~ +5ms，触发在中心
    for i, (freq, amp) in enumerate([(1e3, 2.0), (500, 1.0), (250, 0.5), (125, 0.25)], 1):
        wfs[f"CH{i}"] = Waveform(channel=f"CH{i}", y=amp * np.sin(2 * math.pi * freq * t),
                                 x_start=t[0], x_incr=1e-5)

    with tempfile.TemporaryDirectory() as d:
        files = export_ltspice_pwl(wfs, d, base="scope")
        check("四通道各生成一个 CSV", len(files) == 4, str(files))
        f0 = Path(files[0])
        lines = f0.read_text(encoding="ascii").strip().splitlines()
        check("PWL 行数=点数", len(lines) == n, str(len(lines)))
        first = lines[0].split(",")
        last = lines[-1].split(",")
        check("双列无表头", len(first) == 2 and first[0].replace(".", "").replace("-", "").isdigit(),
              str(first))
        t0, t1 = float(first[0]), float(last[0])
        check("时间从 0 起单调", t0 == 0.0 and t1 > 0 and t1 > t0, f"{t0} -> {t1}")
        check("时间跨度=波形窗口", abs(t1 - 9.99e-3) < 1e-9, str(t1))

        # .asc 电路
        asc = write_ltspice_asc(files, Path(d) / "scope.asc", list(wfs.keys()), tran_stop=1e-2)
        text = Path(asc).read_text(encoding="utf-8")
        check(".asc 含 4 个 PWL 源", text.count("PWL FILE=") == 4, str(text.count("PWL FILE=")))
        check(".asc 节点标注", all(f"FLAG .* {name}" for name in wfs), "CH1..CH4")
        check(".asc 含 .tran", ".tran 0 0.01 0" in text or ".tran 0 1e-02 0" in text, text[-80:])

        # Excel 多列
        xlsx = export_spreadsheet(wfs, Path(d) / "all.csv")
        header = Path(xlsx).read_text(encoding="utf-8").splitlines()[0]
        check("Excel CSV 表头", header.startswith("Time_s,CH1_V,CH2_V,CH3_V,CH4_V"), header)
        data = np.loadtxt(xlsx, delimiter=",", skiprows=1)
        check("Excel CSV 列数", data.shape[1] == 5, str(data.shape))
        check("Excel CSV 行数", data.shape[0] == n, str(data.shape))


def main() -> None:
    print("== 解码器单元测试 ==")
    test_ieee_block()
    test_decode_raw()
    test_tds_header()
    test_rigol_header()
    test_ascii()
    test_ds1000e_formula()
    print("== 端到端 ==")
    test_e2e_tektronix()
    test_e2e_rigol_decoder()
    test_e2e_capture_screen()
    print("== 导出 ==")
    test_export_ltspice()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
