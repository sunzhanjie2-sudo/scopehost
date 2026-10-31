"""波形导出模块。

面向 LTSpice / Excel 的 CSV 导出：

1. export_ltspice_pwl —— 每通道一个双列无表头 CSV（time, voltage），
   即 LTSpice PWL file 格式。四路波形 = 四个文件，LTSpice 中用四个
   PWL 电压源加载。关键约束：
     - 只有两列（LTSpice PWL 不认多列表头）
     - 时间从 0 开始单调递增（示波器时间轴以触发为中心含负值，需平移）
     - 数值用 %.15g 保证双精度（深存储大点数不受损）

2. write_ltspice_asc —— 生成 LTSpice 原理图：4 个 PWL 电压源分别接到
   CH1..CH4 节点 + .tran 仿真命令，打开即用。

3. export_spreadsheet —— 单文件多列带表头（Time, CH1_V, ...），
   Excel/Origin 用；时间轴以第一通道为准，其余通道插值对齐。

4. export_single —— 单通道两列（复用 Waveform.to_csv）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .model import Waveform

# LTSpice PWL 数值格式：保留双精度，兼容 SPICE 数值语法
_FMT = "{:.15g}"


def _pwl_rows(wf: Waveform) -> np.ndarray:
    """波形 → (时间从 0 起, 电压) 两列数组。"""
    t = wf.x - wf.x_start  # LTSpice 要求时间 >= 0 且单调
    return np.column_stack((t, wf.y))


def export_ltspice_pwl(
    waveforms: dict[str, Waveform],
    out_dir: str | Path,
    base: str = "scope",
) -> list[str]:
    """每通道一个 PWL CSV（time,voltage 无表头），返回生成的文件列表。

    waveforms: {通道名: Waveform}，如 {"CH1": wf, "CH2": wf, ...}
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    for name in sorted(waveforms):
        wf = waveforms[name]
        if wf is None or wf.points < 2:
            continue
        path = out_dir / f"{base}_{name.lower()}.csv"
        rows = _pwl_rows(wf)
        with open(path, "w", encoding="ascii") as f:
            for t, v in rows:
                f.write(f"{_FMT.format(t)},{_FMT.format(v)}\n")
        files.append(str(path))
    return files


def write_ltspice_asc(
    files: list[str],
    asc_path: str | Path,
    ch_names: list[str],
    tran_stop: float | None = None,
) -> str:
    """生成 LTSpice 原理图：每个 PWL 文件一个电压源 → CHx 节点。

    电压源上引脚 WIRE 拉到 FLAG(CHx)，下引脚接 0(GND)；4 个源共享 GND。
    返回 .asc 文件路径。
    """
    asc_path = Path(asc_path)
    lines = ["Version 4", "SHEET 1 1200 900"]
    y = 128
    for i, (name, fpath) in enumerate(zip(ch_names, files)):
        x = 96
        path_asc = str(Path(fpath)).replace("\\", "/")  # LTSpice 接受正斜杠
        lines.append(f"SYMBOL voltage {x} {y} R0")
        lines.append(f"SYMATTR InstName V{i + 1}")
        lines.append(f"SYMATTR Value PWL FILE={path_asc}")
        # 正极 → 节点名
        lines.append(f"WIRE {x} {y} {x + 64} {y}")
        lines.append(f"FLAG {x + 64} {y} {name}")
        # 负极 → GND
        lines.append(f"WIRE {x} {y + 112} {x + 64} {y + 112}")
        lines.append(f"FLAG {x + 64} {y + 112} 0")
        y += 160
    if tran_stop is None:
        tran_stop = 20e-3
    lines.append(f"TEXT 24 24 Left 2 !.tran 0 {_FMT.format(tran_stop)} 0")
    asc_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(asc_path)


def export_spreadsheet(
    waveforms: dict[str, Waveform],
    path: str | Path,
    delimiter: str = ",",
) -> str:
    """单文件多列 CSV（表头：Time, CH1_V, CH2_V, ...），Excel/Origin 用。

    时间轴以第一个波形为准，其余波形插值对齐（不同通道点数/起点不同也安全）。
    """
    items = [(n, wf) for n, wf in waveforms.items()
             if wf is not None and wf.points >= 2]
    if not items:
        raise ValueError("没有可导出的波形数据")
    ref_name, ref = items[0]
    t = ref.x
    header = ["Time_s"] + [f"{n}_V" for n, _ in items]
    cols: list[np.ndarray] = [t]
    for n, wf in items:
        if (
            abs(wf.x_start - ref.x_start) < 1e-12
            and abs(wf.x_incr - ref.x_incr) < 1e-12
            and wf.points == ref.points
        ):
            cols.append(wf.y)
        else:
            cols.append(np.interp(t, wf.x, wf.y, left=np.nan, right=np.nan))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savetxt(path, np.column_stack(cols), delimiter=delimiter,
               header=delimiter.join(header), comments="")
    return str(path)
