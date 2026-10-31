"""ScopeHost 示波器上位机入口。

用法：
    python run.py             # 启动 GUI（手动连接）
    python run.py --demo      # 启动 GUI 并自动连接内置模拟器（无真机演示）
    python run.py --headless  # 离屏自检：连接模拟器采集一帧并渲染 PNG 后退出
"""
from __future__ import annotations

import argparse
import os
import sys
import multiprocessing


def main() -> None:
    parser = argparse.ArgumentParser(description="ScopeHost 通用示波器上位机")
    parser.add_argument("--demo", action="store_true", help="自动启动内置模拟器并连接")
    parser.add_argument("--headless", action="store_true", help="离屏自检（渲染一帧 PNG 后退出）")
    parser.add_argument("--visa", default="", help="启动后连接指定真实 VISA 资源，也支持 --headless")
    parser.add_argument("--out", default="preview.png", help="--headless 的输出图片路径")
    parser.add_argument("--diag", action="store_true", help="打印仪器连接环境诊断后退出")
    parser.add_argument("--scan-visa", action="store_true", help="隔离进程扫描 VISA，将 JSON 结果写入 --out")
    parser.add_argument("--visa-backend", default="", choices=("", "@ivi", "@py"), help="指定 VISA 后端；默认自动选择")
    args = parser.parse_args()

    if args.scan_visa:
        import json
        from pathlib import Path
        from scopeapp.visa_process import VisaProcessTransport

        try:
            result = VisaProcessTransport(backend_spec=args.visa_backend).scan_report()
        except Exception as error:
            result = {"error": str(error), "backend_requested": args.visa_backend or "auto"}
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        sys.exit(1 if "error" in result else 0)

    if args.diag:
        from scopeapp.transport import visa_diagnostic

        print(visa_diagnostic())
        return

    if args.headless:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _headless_selfcheck(args.out, args.visa, args.visa_backend)
        return

    from PySide6.QtWidgets import QApplication

    from scopeapp.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    if args.demo and args.visa:
        parser.error("--demo 与 --visa 不能同时使用")
    win = MainWindow(auto_simulator=args.demo, visa_resource=args.visa, visa_backend=args.visa_backend)
    win.show()
    sys.exit(app.exec())


def _headless_selfcheck(out_path: str, visa_resource: str = "", visa_backend: str = "") -> None:
    """离屏自检：模拟器或真机 → 会话 → 采集 → 渲染 PNG。"""
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from scopeapp.session import ScopeSession
    from scopeapp.simulator import SimulatorServer
    from scopeapp.ui.scope_view import ScopeView

    app = QApplication(sys.argv)
    QGuiApplication.setApplicationName("scope-headless")

    sess = ScopeSession()
    srv = None
    try:
        if visa_resource:
            ad = sess.connect_visa(visa_resource, backend_spec=visa_backend)
            print(f"已连接真机: {ad.identity.full}  适配器: {ad.name}")
            n_ch = ad.channel_count
            view = ScopeView()
            view.data_source = f"真实 USB · {ad.identity.model}"
            view.resize(900, 480)
            channels = {f"CH{n}": ad.read_channel(n) for n in range(1, n_ch + 1)}
            ad.update_enabled_cache(channels)
            waveforms = ad.fetch_all()
            tb = ad.read_timebase()
            trig = ad.read_trigger()
            view.set_data({k: v for k, v in waveforms.items() if v is not None},
                          channels, tb, trig)
            view.grab().save(out_path)
            print(f"时域渲染(真机): {out_path}")
            for name, w in waveforms.items():
                if w is not None:
                    print(f"  {name}: {w.points} 点 Vpp={w.vpp:.4f}V")
            # 抓屏：优先真机抓屏，否则数据重建
            try:
                data = ad.capture_screen()
                src = "真机抓屏"
            except NotImplementedError:
                data = ad.render_screen_snapshot()
                src = "数据重建"
            img_path = out_path.replace(".png", "_screen.png")
            with open(img_path, "wb") as f:
                f.write(data)
            print(f"屏幕截图({src}): {img_path}")
        else:
            srv = SimulatorServer(port=5027)
            srv.start()
            srv.scope.channels["CH1"]["signal"].freq = 1_000.0
            srv.scope.channels["CH1"]["signal"].amp = 2.0
            srv.scope.channels["CH2"]["signal"].kind = "square"
            srv.scope.channels["CH2"]["signal"].freq = 300.0
            srv.scope.channels["CH2"]["signal"].amp = 1.0
            srv.scope.channels["CH3"]["signal"].kind = "chirp"
            srv.scope.channels["CH3"]["signal"].freq = 100.0
            srv.scope.channels["CH3"]["signal"].amp = 1.5

            ad = sess.connect_simulator(5027)
            print(f"已连接: {ad.identity.full}  适配器: {ad.name}")

            view = ScopeView()
            view.data_source = "模拟信号 · SIMULATION"
            view.resize(900, 480)
            waveforms = ad.fetch_all()
            channels = {f"CH{n}": ad.read_channel(n) for n in range(1, 5)}
            tb = ad.read_timebase()
            trig = ad.read_trigger()
            view.set_data({k: v for k, v in waveforms.items() if v is not None},
                          channels, tb, trig)
            view.grab().save(out_path)
            print(f"时域渲染: {out_path}")

            view.fft_enabled = True
            view.grab().save(out_path.replace(".png", "_fft.png"))
            print(f"FFT 渲染: {out_path.replace('.png', '_fft.png')}")

        # 单通道波形与测量
        first_channel = next((int(name[2:]) for name in waveforms), None)
        if first_channel is None:
            raise RuntimeError("没有启用且可读取的通道")
        wf = waveforms[f"CH{first_channel}"]
        from scopeapp.measurements import measure
        ms = {m.name: m.value for m in measure(wf)}
        print(f"CH{first_channel} 测量: " + ", ".join(f"{k}={v:.4g}" for k, v in list(ms.items())[:4]))
        sess.disconnect()
    finally:
        sess.disconnect()
        if srv is not None:
            srv.stop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
