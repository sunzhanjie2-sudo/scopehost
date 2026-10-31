"""GUI 导出功能冒烟：模拟器四通道 → LTSpice PWL + .asc + Excel CSV。"""
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\CODE\scope")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from scopeapp.export import export_ltspice_pwl, export_spreadsheet, write_ltspice_asc
from scopeapp.ui.main_window import MainWindow

app = QApplication(sys.argv)
win = MainWindow(auto_simulator=True)
win.show()
state = {"n": 0}


def tick():
    try:
        state["n"] += 1
        if state["n"] < 8:
            return
        wf = win._export_waveforms()
        print("导出波形通道:", list(wf.keys()))
        assert len(wf) == 4, "模拟器应有 4 通道"

        with tempfile.TemporaryDirectory() as d:
            files = export_ltspice_pwl(wf, d, base="scope")
            ch_names = [Path(f).stem.replace("scope_", "").upper() for f in files]
            asc = write_ltspice_asc(files, Path(d) / "scope_ltspice.asc", ch_names,
                                    tran_stop=next(iter(wf.values())).duration)
            print("PWL 文件:", [Path(f).name for f in files])
            print(".asc 文件:", Path(asc).name)
            text = Path(asc).read_text(encoding="utf-8")
            assert text.count("PWL FILE=") == 4, "asc 应含 4 个 PWL 源"
            assert "CH1" in text and "CH4" in text, "asc 应含 CH1..CH4 节点"
            # 检查 CSV 时间从 0 起
            first = Path(files[0]).read_text(encoding="ascii").splitlines()
            assert float(first[0].split(",")[0]) == 0.0, "时间应从 0 开始"
            print("PWL 首行:", first[0], "| 末行:", first[-1])

            xlsx = export_spreadsheet(wf, Path(d) / "all.csv")
            print("Excel CSV 表头:", Path(xlsx).read_text(encoding="utf-8").splitlines()[0])
        print("GUI 导出冒烟 OK")
        win.close()
        app.quit()
    except Exception as e:
        print("冒烟异常:", type(e).__name__, e)
        app.quit()


t = QTimer()
t.timeout.connect(tick)
t.start(400)
QTimer.singleShot(15000, app.quit)
app.exec()
