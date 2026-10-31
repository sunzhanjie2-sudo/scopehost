"""多通道示波器工作台。设备通信全部在后台串行执行。"""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import csv
import json
import math
import time
import numpy as np
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QEventLoop
from PySide6.QtGui import QColor, QKeySequence, QAction
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton,
    QVBoxLayout, QWidget,
)
from .. import __version__
from ..adapters import list_adapters
from ..archive import load_archive, save_archive
from ..measurements import fft_spectrum, measure
from ..model import ChannelSettings, Coupling, TriggerSettings
from ..processing import FrameAverager, math_waveform, moving_average
from ..session import ScopeSession
from ..simulator import SimulatorServer
from ..visa_process import VisaProcessTransport
from ..waveform import sample_bounds
from .scope_view import CHANNEL_COLORS, TIMEBASE_STEPS, VOLT_STEPS, ScopeView, _fmt_eng
from .worker import AcquisitionWorker
from .fonts import ensure_fonts
from .numeric_spin import NumericSpinBox

STYLE = """
QWidget {background:#111923;color:#d5dfec;font-family:'Microsoft YaHei UI';font-size:12px;}
QLabel#brand {font-size:25px;font-weight:700;color:#edf5ff;}
QLabel#muted {color:#8294aa;font-size:11px;}
QLabel#state {color:#5de0b4;background:#153a33;border:1px solid #285a4b;border-radius:6px;padding:7px 14px;font-weight:700;}
QPushButton,QToolButton {background:#1c2a3a;border:1px solid #324357;border-radius:6px;padding:7px 12px;}
QPushButton:hover,QToolButton:hover {background:#293e54;border-color:#6a8bae;}
QPushButton:checked {background:#164839;border-color:#38bd90;color:#81f5c5;}
QPushButton:disabled,QComboBox:disabled,QDoubleSpinBox:disabled {color:#596b7c;}
QPushButton#primary {background:#2666c9;color:white;border-color:#4787e7;}
QPushButton#single {color:#ffcc73;border-color:#745632;}
QComboBox,QSpinBox,QDoubleSpinBox,QLineEdit {background:#0d1520;border:1px solid #344457;border-radius:4px;padding:5px;min-height:20px;}
QComboBox::drop-down {width:20px;border:0;}
QComboBox QAbstractItemView {background:#1b2939;selection-background-color:#275788;}
QGroupBox {border:1px solid #2c3c4f;border-radius:7px;margin-top:14px;padding:14px 10px 8px;font-weight:600;}
QGroupBox::title {subcontrol-origin:margin;subcontrol-position:top left;left:12px;padding:0 5px;color:#a4b8d0;}
QTabWidget::pane {border:1px solid #2c3c4f;border-radius:7px;}
QTabBar::tab {background:#172231;color:#8fa5bd;padding:10px 13px;}
QTabBar::tab:selected {color:#ecf5ff;background:#26394d;border-bottom:2px solid #70b0ff;}
QTableWidget {background:#0e1722;border:1px solid #2b3b4e;gridline-color:#243448;selection-background-color:#243e58;}
QHeaderView::section {background:#192637;color:#9fb4cd;padding:7px;border:0;border-bottom:1px solid #34485d;}
QMenuBar {background:#111923;color:#99adc4;}
QMenuBar::item:selected,QMenu::item:selected {background:#2a4561;}
QMenu {background:#1b2939;border:1px solid #3a4c61;}
QStatusBar {color:#8fa5bd;border-top:1px solid #26374a;}
QSplitter::handle {background:#243448;}
QScrollArea {border:0;}
QScrollBar:vertical {background:#111923;width:7px;}
QScrollBar::handle:vertical {background:#435a72;min-height:30px;border-radius:3px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}
QCheckBox {spacing:7px;}
"""

def row(*widgets):
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    for item in widgets:
        layout.addWidget(item)
    return widget

def combo(items):
    widget = QComboBox()
    for label, value in items:
        widget.addItem(label, value)
    return widget

def spin(low=-100, high=100, decimals=6):
    widget = NumericSpinBox()
    widget.setRange(low, high)
    widget.setDecimals(decimals)
    widget.setSingleStep(0.1)
    widget.setKeyboardTracking(False)
    return widget

def wait_thread_responsive(thread, timeout_ms=7000):
    """停止时继续处理 Qt 事件，不在界面线程等待原生 VISA 调用。"""
    if not thread.isRunning():
        return True
    loop, timer = QEventLoop(), QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    thread.finished.connect(loop.quit)
    timer.start(timeout_ms)
    if thread.isRunning():
        loop.exec()
    timer.stop()
    thread.finished.disconnect(loop.quit)
    return not thread.isRunning()


class VisaScanWorker(QThread):
    resourcesReady = Signal(object)
    failed = Signal(str)

    def __init__(self, parent=None, backend_spec=""):
        super().__init__(parent)
        self.transport = VisaProcessTransport(backend_spec=backend_spec)

    def cancel(self):
        self.transport.cancel()

    def run(self):
        try:
            self.resourcesReady.emit(self.transport.scan_report())
        except Exception as error:
            self.failed.emit(str(error))


class ConnectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"连接示波器 · v{__version__}")
        self.setMinimumWidth(600)
        self._scanner = None
        self._scan_report = {}
        form = QFormLayout(self)
        self.method = combo([("VISA / USB-TMC（真实设备）", 2), ("TCP / LAN（真实设备）", 1), ("模拟器（生成演示信号）", 0)])
        self.host = QLineEdit("192.168.1.50")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(5025)
        self.visa_res = QComboBox()
        self.visa_res.setEditable(True)
        self.visa_res.lineEdit().setPlaceholderText("选择 USB 仪器资源或输入完整地址")
        self.visa_backend = combo([("自动选择", ""), ("系统 VISA（NI / Keysight / Tek 等）", "@ivi"),
                                   ("内置 pyvisa-py（需要兼容的 USB 驱动）", "@py")])
        self.show_serial = QCheckBox("显示串口资源（仅供诊断）")
        self.show_serial.toggled.connect(self._populate_resources)
        refresh = QPushButton("扫描")
        refresh.setMaximumWidth(90)
        self.btn_scan = refresh
        refresh.clicked.connect(self._scan)
        self.adapter = QComboBox()
        self.adapter.addItems(["自动识别"] + list_adapters())
        form.addRow("连接方式", self.method)
        form.addRow("主机 / 端口", row(self.host, self.port))
        form.addRow("VISA 驱动后端", self.visa_backend)
        form.addRow("VISA 资源", row(self.visa_res, refresh))
        form.addRow(self.show_serial)
        form.addRow("设备适配器", self.adapter)
        self.tip = QLabel("VISA 资源可手动输入；USB 连接需要相应 VISA / USBTMC 驱动。")
        self.tip.setWordWrap(True)
        self.tip.setTextFormat(Qt.PlainText)
        self.tip.setObjectName("muted")
        form.addRow(self.tip)
        self.backend_info = QLabel("扫描后显示实际 VISA 库与 USB / 串口数量。")
        self.backend_info.setWordWrap(True)
        self.backend_info.setTextFormat(Qt.PlainText)
        self.backend_info.setObjectName("muted")
        self.backend_info.setMinimumHeight(40)
        form.addRow(self.backend_info)
        self.btn_diagnostic = QPushButton("导出连接诊断")
        self.btn_diagnostic.clicked.connect(self._export_diagnostic)
        form.addRow(self.btn_diagnostic)
        ok, cancel = QPushButton("连接"), QPushButton("取消")
        ok.setObjectName("primary")
        ok.clicked.connect(self._accept_selection)
        cancel.clicked.connect(self.reject)
        form.addRow(row(ok, cancel))
        self.method.currentIndexChanged.connect(self._method_changed)
        self._method_changed()
        QTimer.singleShot(0, self._scan)
        self.adapter.currentTextChanged.connect(self._adapter_changed)
        self.visa_backend.currentIndexChanged.connect(self._backend_changed)
        self.finished.connect(self._cancel_scan)

    def _adapter_changed(self, name):
        from ..adapters.registry import adapters_registry

        adapter = adapters_registry.by_name(name)
        port = getattr(adapter, "default_tcp_port", 5025)
        if self.port.value() in (5025, 10002):
            self.port.setValue(port)
        if port == 10002:
            self.tip.setText("DLM3000：网口使用 Socket 接口、端口 10002；USB 须为 VISA 可见的 USBTMC 设备。")

    def _scan(self):
        if not self.isVisible() or (self._scanner and self._scanner.isRunning()):
            return
        self.btn_scan.setEnabled(False)
        self.visa_backend.setEnabled(False)
        self.tip.setText("正在扫描 VISA…（最多 8 秒，可直接输入地址或取消）")
        if self._scanner:
            self._scanner.deleteLater()
        self._scanner = VisaScanWorker(self, self.visa_backend.currentData())
        self._scanner.resourcesReady.connect(self._scan_finished)
        self._scanner.failed.connect(self._scan_failed)
        self._scanner.finished.connect(self._scan_controls_ready)
        self._scanner.start()

    def _scan_controls_ready(self):
        self.btn_scan.setEnabled(True)
        self.visa_backend.setEnabled(self.method.currentData() == 2)

    def _backend_changed(self, *_):
        self._scan_report = {}
        self._populate_resources()
        QTimer.singleShot(0, self._scan)

    def _scan_finished(self, report):
        self._scan_report = report
        self._populate_resources()
        resources = report["resources"]
        usb_count = len([r for r in resources if r.upper().startswith("USB")])
        serial_count = len([r for r in resources if r.upper().startswith("ASRL")])
        self.backend_info.setText(f"实际后端：{report['backend']}\n"
                                  f"PyVISA {report['pyvisa_version']} · {report['python_bits']} 位 · "
                                  f"USB {usb_count} · 串口 {serial_count}")
        if usb_count:
            self.tip.setText(f"找到 {usb_count} 个 USB 仪器资源；请选择与示波器对应的 USB 地址。")
        else:
            self.tip.setText(f"没有找到 USB 示波器（串口 {serial_count} 个，不能代替 USB 仪器）。\n"
                             "新电脑需安装兼容的 VISA / USBTMC 驱动；可先在 NI MAX 或厂商连接工具中检查设备。")
        self.adjustSize()

    def _scan_failed(self, message):
        self._scan_report = {"error": message, "backend_requested": self.visa_backend.currentData() or "auto",
                             "resources": []}
        self._populate_resources()
        self.tip.setText(message)
        self.backend_info.setText("扫描失败与没有设备分别报告；可以导出诊断，或改选已安装的系统 VISA 后端。")
        self.adjustSize()

    def _populate_resources(self, *_):
        current = self.visa_res.currentText()
        resources = self._scan_report.get("resources", [])
        self.visa_res.clear()
        for resource in sorted(resources, key=lambda item: (not item.upper().startswith("USB"), item)):
            if resource.upper().startswith("ASRL"):
                if self.show_serial.isChecked():
                    self.visa_res.addItem(f"{resource} · 串口，非 USB 仪器", resource)
                    item = self.visa_res.model().item(self.visa_res.count() - 1)
                    item.setEnabled(False)
                continue
            self.visa_res.addItem(resource, resource)
        if current and not current.upper().startswith("ASRL"):
            self.visa_res.setEditText(current)
        elif not any(not r.upper().startswith("ASRL") for r in resources):
            self.visa_res.setCurrentIndex(-1)

    def _accept_selection(self):
        if self.method.currentData() == 2:
            resource = self.visa_res.currentText().strip()
            if not resource:
                self.tip.setText("请选择 USB 仪器资源；当前没有可连接的仪器地址。可导出诊断或使用 LAN。")
                return
            if resource.upper().startswith("ASRL"):
                self.tip.setText("ASRL 是串口资源，不是 USBTMC 示波器；当前连接窗口请使用 USB 仪器地址或 LAN。")
                return
        self.accept()

    def _export_diagnostic(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出连接诊断", "ScopeHost_VISA诊断.json", "JSON (*.json)")
        if not path:
            return
        report = {"scopehost_version": __version__, "selected": self.selected(), "scan": self._scan_report}
        try:
            Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            self.tip.setText("连接诊断已保存；包含实际 VISA 后端、资源列表和扫描错误。")
        except OSError as error:
            self.tip.setText(f"保存诊断失败：{error}")

    def _cancel_scan(self, *_):
        if self._scanner and self._scanner.isRunning():
            self._scanner.cancel()
            wait_thread_responsive(self._scanner, 1000)

    def _method_changed(self):
        self.host.setEnabled(self.method.currentData() == 1)
        self.port.setEnabled(self.method.currentData() == 1)
        self.visa_res.setEnabled(self.method.currentData() == 2)
        self.visa_backend.setEnabled(self.method.currentData() == 2 and not (self._scanner and self._scanner.isRunning()))
        self.show_serial.setEnabled(self.method.currentData() == 2)

    def selected(self):
        return {"method": self.method.currentData(), "host": self.host.text().strip(),
                "port": self.port.value(), "visa": self.visa_res.currentText().strip(),
                "visa_backend": self.visa_backend.currentData(),
                "adapter": None if self.adapter.currentIndex() == 0 else self.adapter.currentText()}

class ConnectionWorker(QThread):
    connected = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, options, parent=None):
        super().__init__(parent)
        self.options = options
        self.session = ScopeSession()

    def cancel(self):
        self.session.cancel_connect()

    def run(self):
        session, simulator = self.session, None
        try:
            opts = self.options
            self.progress.emit("正在打开通信接口并读取设备身份…")
            if opts["method"] == 0:
                simulator = SimulatorServer(port=0)
                simulator.scope.hscale = 200e-6
                signals = [("sine", 1000, 1.8), ("square", 500, 1.2), ("ramp", 1000, 1.5), ("sine", 2000, 0.8)]
                for n, (kind, freq, amp) in enumerate(signals, 1):
                    ch = simulator.scope.channels[f"CH{n}"]
                    ch["signal"].kind, ch["signal"].freq, ch["signal"].amp = kind, freq, amp
                    ch["offset"] = [-2.4, -0.8, 0.8, 2.4][n - 1]
                simulator.start()
                session.connect_simulator(simulator.port)
            elif opts["method"] == 1:
                session.connect_tcp(opts["host"], opts["port"], opts["adapter"])
            else:
                if not opts["visa"]:
                    raise ValueError("请输入 VISA 资源地址")
                if opts["visa"].upper().startswith("ASRL"):
                    raise ValueError("ASRL 是串口资源，不是 USB 示波器地址；请选择 USB…::INSTR 或 LAN")
                session.connect_visa(opts["visa"], opts["adapter"], opts.get("visa_backend", ""))
            from ..adapters.tektronix import TektronixAdapter
            if isinstance(session.adapter, TektronixAdapter):
                channels = {}
                for n in range(1, session.adapter.channel_count + 1):
                    self.progress.emit(f"已识别 {session.adapter.identity.model}，正在核验 CH{n} 设置…")
                    channels[f"CH{n}"] = session.adapter.read_channel(n)
                self.progress.emit("正在核验时基与边沿触发…")
                session.initial_settings = {"channels": channels, "timebase": session.adapter.read_timebase(),
                                            "trigger": session.adapter.read_trigger()}
                session.adapter.update_enabled_cache(channels)
            session._check_cancelled()
            self.connected.emit((session, simulator))
        except Exception as error:
            session.disconnect()
            if simulator:
                simulator.stop()
            self.failed.emit(str(error))

class ArchiveWriter(QThread):
    saved = Signal(str)
    failed = Signal(str)

    def __init__(self, path, waveforms, settings, identity, processing, parent=None):
        super().__init__(parent)
        # Each acquisition owns its arrays; retaining this frame is a stable snapshot.
        self.arguments = (path, dict(waveforms), settings, identity, processing)

    def run(self):
        try:
            self.saved.emit(save_archive(*self.arguments, require_complete=True))
        except Exception as error:
            self.failed.emit(str(error))


class MainWindow(QMainWindow):
    def __init__(self, auto_simulator=False, visa_resource="", visa_backend=""):
        super().__init__()
        ensure_fonts()
        self.setWindowTitle(f"ScopeHost · 示波器工作台 v{__version__}")
        self.resize(1440, 930)
        self.setMinimumSize(1050, 720)
        self.setStyleSheet(STYLE)
        self.session = ScopeSession()
        self.worker = self.connector = self._sim = None
        self._settings, self._raw_waveforms, self._last_measurements = {}, {}, {}
        self._frame_settings = {}
        self._measurement_cache = {}
        self._archive_writer = None
        self._averager = FrameAverager()
        self._averaged_waveforms = {}
        self._view_position = 0.0
        self._device_position = None
        self._source = "未连接 · 无采样数据"
        self._zoom_syncing = False
        self._pending_numeric = {}
        self._frames, self._last_frame_at = 0, 0.0
        self._state, self._shot_path = "OFFLINE", ""
        self._disconnecting = False
        self._close_requested = False
        self._connection_cancelled = False
        self._real_refresh_delay = 1000
        self._last_acquisition_ms = 0
        self._connection_timer = QTimer(self)
        self._connection_timer.setSingleShot(True)
        self._connection_timer.timeout.connect(self._cancel_connection_timeout)
        self._build_ui()
        if auto_simulator:
            self._start_simulator_and_connect()
        elif visa_resource:
            QTimer.singleShot(0, lambda: self._do_connect({"method": 2, "visa": visa_resource, "adapter": None,
                                                         "visa_backend": visa_backend}))

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 12, 18, 8)
        layout.setSpacing(12)
        self.setCentralWidget(root)
        header = QHBoxLayout()
        title = QLabel("ScopeHost")
        title.setObjectName("brand")
        header.addWidget(title)
        subtitle = QLabel("示波器工作台  /  ACQUIRE · ANALYZE · CAPTURE")
        subtitle.setObjectName("muted")
        header.addWidget(subtitle)
        header.addStretch()
        self.btn_connect = QPushButton("连接设备")
        self.btn_connect.setObjectName("primary")
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_demo = QPushButton("模拟信号演示")
        self.btn_demo.clicked.connect(self._start_simulator_and_connect)
        self.btn_disconnect = QPushButton("断开")
        self.btn_disconnect.clicked.connect(self._disconnect)
        for widget in (self.btn_demo, self.btn_connect, self.btn_disconnect):
            header.addWidget(widget)
        layout.addLayout(header)
        toolbar = QHBoxLayout()
        self.btn_run = QPushButton("▶ 运行")
        self.btn_run.setCheckable(True)
        self.btn_run.clicked.connect(self._on_run_toggle)
        self.btn_single = QPushButton("Single · 单次")
        self.btn_single.setObjectName("single")
        self.btn_single.clicked.connect(self._on_single)
        self.btn_force = QPushButton("强制触发")
        self.btn_force.clicked.connect(lambda: self._submit("force_trigger"))
        self.btn_auto = QPushButton("Auto Set")
        self.btn_auto.clicked.connect(lambda: self._submit("autoset"))
        self.btn_fft = QPushButton("FFT 频谱")
        self.btn_fft.setCheckable(True)
        self.btn_fft.toggled.connect(self._toggle_fft)
        self.btn_zoom = QPushButton("ZOOM 放大")
        self.btn_zoom.setCheckable(True)
        self.btn_zoom.toggled.connect(self._toggle_zoom)
        self.btn_stacked = QPushButton("通道堆叠")
        self.btn_stacked.setCheckable(True)
        self.btn_stacked.toggled.connect(self._toggle_stacked)
        self.btn_cursor = QPushButton("游标")
        self.btn_cursor.setCheckable(True)
        self.btn_reference = QPushButton("保留参考")
        self.btn_reference.setCheckable(True)
        self.btn_reference.toggled.connect(self._reference)
        for widget in (self.btn_run, self.btn_single, self.btn_force, self.btn_auto, self.btn_zoom, self.btn_stacked, self.btn_fft, self.btn_cursor, self.btn_reference):
            toolbar.addWidget(widget)
        toolbar.addStretch()
        image = QPushButton("保存软件截图")
        image.clicked.connect(self._save_plot)
        self.btn_export = QToolButton()
        self.btn_export.setText("保存 / 导出 ▾")
        self.btn_export.setPopupMode(QToolButton.InstantPopup)
        exports = QMenu(self)
        for label, callback in [("原始波形归档 · NPZ", self._save_archive), ("当前分析波形 · CSV", self._on_export_spreadsheet),
                                ("原始多通道 · CSV", self._export_raw), ("选中通道 · CSV", self._on_export),
                                ("ZOOM 区间 · CSV", self._export_zoom),
                                ("FFT 频谱 · CSV", self._export_fft), ("测量结果 · CSV", self._export_measurements),
                                ("LTSpice PWL + 电路", self._on_export_ltspice)]:
            exports.addAction(label, callback)
        self.btn_export.setMenu(exports)
        toolbar.addWidget(image)
        toolbar.addWidget(self.btn_export)
        layout.addLayout(toolbar)
        info = QHBoxLayout()
        self.lbl_state = QLabel("OFFLINE")
        self.lbl_state.setObjectName("state")
        self.lbl_source = QLabel(self._source)
        self.lbl_source.setStyleSheet("color:#ffcc73;font-weight:700")
        self.lbl_status = QLabel("未连接设备 · 请连接真实示波器")
        self.lbl_status.setObjectName("muted")
        self.lbl_acquisition = QLabel("-- Sa/s  ·  -- points")
        self.lbl_acquisition.setObjectName("muted")
        info.addWidget(self.lbl_state)
        info.addWidget(self.lbl_source)
        info.addWidget(self.lbl_status, 1)
        info.addWidget(self.lbl_acquisition)
        layout.addLayout(info)
        splitter = QSplitter(Qt.Horizontal)
        display = QWidget()
        display_layout = QVBoxLayout(display)
        display_layout.setContentsMargins(0, 0, 0, 0)
        self.scope_view = ScopeView()
        self.scope_view.setMinimumHeight(180)
        self.scope_view.timebaseChanged.connect(self._on_timebase_changed)
        self.scope_view.positionChanged.connect(self._on_position_changed)
        self.scope_view.zoomRangeChanged.connect(self._zoom_range_changed)
        self.scope_view.zoomResetRequested.connect(self._reset_zoom)
        self.zoom_view = ScopeView()
        self.zoom_view.local_zoom = True
        self.zoom_view.setMinimumHeight(180)
        self.zoom_view.timebaseChanged.connect(self._zoom_scale_changed)
        self.zoom_view.positionChanged.connect(lambda value: self.spn_zoom_center.setValue(value))
        self.zoom_view.zoomResetRequested.connect(self._reset_zoom)
        self.zoom_view.setToolTip("滚轮局部放大；拖动平移局部窗口；双击复位。ZOOM 仅显示已有采样，不增加采样点。")
        self.zoom_view.hide()
        self.scope_view.cursorsChanged.connect(lambda values: self._sync_cursors(self.scope_view, self.zoom_view, values))
        self.zoom_view.cursorsChanged.connect(lambda values: self._sync_cursors(self.zoom_view, self.scope_view, values))
        self.btn_cursor.toggled.connect(self._toggle_cursors)
        self.fft_view = ScopeView()
        self.fft_view.setMinimumHeight(140)
        self.fft_view.set_fft_enabled(True)
        self.fft_view.hide()
        self.plots = QSplitter(Qt.Vertical)
        self.plots.setChildrenCollapsible(False)
        self.plots.addWidget(self.scope_view)
        self.plots.addWidget(self.zoom_view)
        self.plots.addWidget(self.fft_view)
        self.plots.setSizes([480, 0, 0])
        self.spn_zoom_factor = spin(1, 1000000, 2)
        self.spn_zoom_factor.setValue(4)
        self.spn_zoom_factor.setSuffix(" ×")
        self.spn_zoom_factor.setMaximumWidth(115)
        self.spn_zoom_center = spin(-1e6, 1e6, 9)
        self.spn_zoom_center.setMaximumWidth(190)
        self.cmb_zoom_voltage = combo([(f"{gain} ×", gain) for gain in (1, 2, 5, 10)])
        self.cmb_zoom_voltage.setMaximumWidth(75)
        self.spn_zoom_factor.valueChanged.connect(self._update_zoom)
        self.spn_zoom_center.valueChanged.connect(self._update_zoom)
        self.cmb_zoom_voltage.currentIndexChanged.connect(self._update_zoom)
        reset_zoom = QPushButton("复位")
        reset_zoom.clicked.connect(self._reset_zoom)
        self.lbl_zoom_info = QLabel("")
        self.lbl_zoom_info.setObjectName("muted")
        self.lbl_zoom_info.setWordWrap(True)
        self.zoom_controls = row(QLabel("ZOOM 横向"), self.spn_zoom_factor, QLabel("中心 / s"), self.spn_zoom_center,
                                 QLabel("电压"), self.cmb_zoom_voltage, reset_zoom, self.lbl_zoom_info)
        self.zoom_controls.hide()
        display_layout.addWidget(self.zoom_controls)
        display_layout.addWidget(self.plots, 1)
        self.lbl_bandwidth = QLabel("硬件带宽限制 · 等待设备设置")
        self.lbl_bandwidth.setObjectName("muted")
        self.lbl_bandwidth.setWordWrap(True)
        display_layout.addWidget(self.lbl_bandwidth)
        self.cmb_cursor_mode = combo([("时间＋电压", "both"), ("时间（垂直线）", "time"), ("电压（水平线）", "voltage")])
        self.cmb_cursor_source = QComboBox()
        self.cmb_cursor_scope = combo([("当前通道", False), ("全部通道", True)])
        for control in (self.cmb_cursor_mode, self.cmb_cursor_source, self.cmb_cursor_scope):
            control.currentIndexChanged.connect(self._cursor_options)
        self.cursor_controls = row(QLabel("光标模式"), self.cmb_cursor_mode, QLabel("电压光标通道"), self.cmb_cursor_source, self.cmb_cursor_scope)
        self.cursor_controls.hide()
        display_layout.addWidget(self.cursor_controls)
        self.lbl_cursor = QLabel("滚轮调节时基 · 拖动平移 · 开启游标后拖动 A / B 测量")
        self.lbl_cursor.setObjectName("muted")
        self.lbl_cursor.setWordWrap(True)
        display_layout.addWidget(self.lbl_cursor)
        self.measure_table = QTableWidget(0, 9)
        self.measure_table.setHorizontalHeaderLabels(["通道", "峰峰值", "有效值", "平均值", "频率", "周期", "占空比", "最大值", "最小值"])
        self.measure_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.measure_table.verticalHeader().hide()
        self.measure_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.measure_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.measure_table.setMaximumHeight(200)
        self.measure_table.setMinimumHeight(165)
        self.measure_table.itemSelectionChanged.connect(self._selected_channel)
        display_layout.addWidget(self.measure_table)
        splitter.addWidget(display)
        splitter.addWidget(self._build_panel())
        splitter.setSizes([1020, 340])
        layout.addWidget(splitter, 1)
        self._device_controls(False)
        self._configure_channels([])
        self.statusBar().showMessage("就绪 · 原始数据与本地分析数据分别保存")
        file_menu = self.menuBar().addMenu("文件")
        for label, callback, shortcut in [("打开波形归档…", self._open_archive, "Ctrl+O"), ("保存原始波形归档…", self._save_archive, "Ctrl+S"),
            ("保存软件截图…", self._save_plot, "Ctrl+Shift+S"), ("设备屏幕截图…", self._on_screenshot, ""),
            ("退出", self.close, "Ctrl+Q")]:
            action = file_menu.addAction(label, callback)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
        self.menuBar().addMenu("帮助").addAction("操作说明", self._help)
        space = QAction(self)
        space.setShortcut(QKeySequence("Space"))
        space.triggered.connect(lambda: self.btn_run.click() if self.btn_run.isEnabled() else None)
        self.addAction(space)

    def _tab(self, tabs, name):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 8, 10, 10)
        scroll.setWidget(content)
        tabs.addTab(scroll, name)
        return layout

    def _build_panel(self):
        tabs = QTabWidget()
        tabs.setMinimumWidth(320)
        tabs.setMaximumWidth(460)
        capture = self._tab(tabs, "采集")
        self.g_time = QGroupBox("水平 / 时基")
        form = QFormLayout(self.g_time)
        self.cmb_timebase = combo([(f"{_fmt_eng(s, 's')}/div", s) for s in TIMEBASE_STEPS])
        self.cmb_timebase.setProperty("scope_scale_unit", "s")
        self.cmb_timebase.setCurrentIndex(TIMEBASE_STEPS.index(1e-3))
        self.cmb_timebase.currentIndexChanged.connect(self._on_tb_combo)
        form.addRow("时基", self.cmb_timebase)
        self.spn_offset = spin(-1e6, 1e6, 9)
        self.spn_offset.valueChanged.connect(self._on_position_changed)
        form.addRow("视图位置 / s", self.spn_offset)
        self.cmb_record_length = combo([("1.25k · 连续", 1250), ("12.5k · 连续", 12500),
                                        ("125k · Stop / Single", 125000), ("1.25M · Stop / Single", 1250000)])
        self.cmb_record_length.currentIndexChanged.connect(lambda: self._submit("set_record_length", self.cmb_record_length.currentData()))
        self.lbl_record_length = QLabel("设备记录长度")
        form.addRow(self.lbl_record_length, self.cmb_record_length)
        self.lbl_record_length.hide()
        self.cmb_record_length.hide()
        reset = QPushButton("回到触发位置")
        reset.clicked.connect(lambda: self._on_position_changed(0))
        form.addRow(reset)
        capture.addWidget(self.g_time)
        transfer = QGroupBox("上位机刷新 / 通信负载")
        transfer_form = QFormLayout(transfer)
        self.spn_refresh = spin(100, 10000, 0)
        self.spn_refresh.setRange(100, 10000)
        self.spn_refresh.setSingleStep(100)
        self.spn_refresh.setSuffix(" ms")
        self.spn_refresh.setValue(1000)
        self.spn_refresh.setKeyboardTracking(False)
        self.spn_refresh.valueChanged.connect(self._refresh_delay_changed)
        transfer_form.addRow("帧后延迟", self.spn_refresh)
        self.lbl_record_mode = QLabel("完整记录 · 原始采样")
        self.lbl_record_mode.setWordWrap(True)
        transfer_form.addRow("Tek 读取方式", self.lbl_record_mode)
        self.lbl_record_mode.hide()
        self.lbl_transfer_info = QLabel("接收并处理一帧后再等待；控制命令可打断等待。")
        self.lbl_transfer_info.setWordWrap(True)
        self.lbl_transfer_info.setObjectName("muted")
        self.lbl_transfer_info.setMinimumHeight(self.lbl_transfer_info.fontMetrics().lineSpacing() * 5)
        transfer_form.addRow(self.lbl_transfer_info)
        capture.addWidget(transfer)
        self.g_trigger = QGroupBox("边沿触发")
        form = QFormLayout(self.g_trigger)
        self.cmb_trig_src = QComboBox()
        self.cmb_trig_src.addItems([f"CH{n}" for n in range(1, 5)])
        self.cmb_trig_src.currentTextChanged.connect(lambda src: self._submit("set_trigger_source", src))
        self.cmb_trig_mode = combo([("Auto · 自动", "AUTO"), ("Normal · 正常", "NORMAL"), ("Single · 单次", "SINGLE")])
        self.cmb_trig_mode.currentIndexChanged.connect(self._on_trigger_mode)
        self.cmb_trig_slope = combo([("↗ 上升沿", "RISE"), ("↘ 下降沿", "FALL")])
        self.cmb_trig_slope.currentIndexChanged.connect(lambda: self._submit("set_trigger_slope", self.cmb_trig_slope.currentData()))
        self.spn_trig_level = spin(-1e4, 1e4)
        self.spn_trig_level.valueChanged.connect(lambda value: self._submit("set_trigger_level", value))
        form.addRow("触发源", self.cmb_trig_src)
        form.addRow("触发模式", self.cmb_trig_mode)
        form.addRow("边沿", self.cmb_trig_slope)
        form.addRow("电平 / V", self.spn_trig_level)
        self.lbl_trigger_tip = QLabel("Single 等待一次有效触发，完成后冻结全部启用通道。等待中可强制触发或停止。")
        self.lbl_trigger_tip.setWordWrap(True)
        self.lbl_trigger_tip.setObjectName("muted")
        form.addRow(self.lbl_trigger_tip)
        capture.addWidget(self.g_trigger)
        g = QGroupBox("本地跨帧平均")
        form = QFormLayout(g)
        self.cmb_average = combo([("关闭", 1), ("4 帧", 4), ("16 帧", 16), ("64 帧", 64)])
        self.cmb_average.currentIndexChanged.connect(self._processing_changed)
        form.addRow("平均次数", self.cmb_average)
        tip = QLabel("用于重复且对齐的波形。Single 显示本次原始记录；不改变设备采集模式。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        form.addRow(tip)
        capture.addWidget(g)
        capture.addStretch()
        channels = self._tab(tabs, "通道")
        self._ch_widgets = {}
        for n in range(1, 5):
            group = QGroupBox(f"CH{n}")
            form = QFormLayout(group)
            cb = QCheckBox("启用通道")
            cb.setChecked(True)
            cb.setStyleSheet(f"color:{CHANNEL_COLORS[n-1]};font-weight:700")
            cb.toggled.connect(lambda on, num=n: self._on_ch_enabled(num, on))
            scale = combo([(f"{_fmt_eng(s, 'V')}/div", s) for s in VOLT_STEPS])
            scale.setProperty("scope_scale_unit", "V")
            scale.setCurrentIndex(VOLT_STEPS.index(1))
            scale.currentIndexChanged.connect(lambda _, num=n: self._on_ch_scale(num))
            offset = spin(-1e4, 1e4)
            offset.valueChanged.connect(lambda value, num=n: self._submit("set_channel_offset", num, value))
            coupling = QComboBox()
            coupling.addItems(["DC", "AC", "GND"])
            coupling.currentTextChanged.connect(lambda value, num=n: self._submit("set_channel_coupling", num, value))
            form.addRow(cb)
            form.addRow("电压刻度", scale)
            form.addRow("偏移 / V", offset)
            form.addRow("耦合", coupling)
            channels.addWidget(group)
            self._ch_widgets[n] = {"group": group, "enabled": cb, "scale": scale, "offset": offset, "coupling": coupling}
        channels.addStretch()
        analysis = self._tab(tabs, "分析")
        g = QGroupBox("数据处理")
        form = QFormLayout(g)
        self.spn_filter = QSpinBox()
        self.spn_filter.setRange(1, 501)
        self.spn_filter.setValue(1)
        self.spn_filter.setKeyboardTracking(False)
        self.spn_filter.valueChanged.connect(self._processing_changed)
        self.cmb_math = combo([("关闭", "off"), ("A − B", "subtract"), ("A + B", "add")])
        self.cmb_math_a = QComboBox()
        self.cmb_math_b = QComboBox()
        for cmb in (self.cmb_math_a, self.cmb_math_b):
            cmb.addItems([f"CH{n}" for n in range(1, 5)])
        self.cmb_math_b.setCurrentIndex(1)
        for cmb in (self.cmb_math, self.cmb_math_a, self.cmb_math_b):
            cmb.currentIndexChanged.connect(self._processing_changed)
        self.cmb_analysis_src = QComboBox()
        self.cmb_analysis_src.addItems([f"CH{n}" for n in range(1, 5)] + ["MATH"])
        self.cmb_analysis_src.currentTextChanged.connect(self._analysis_options)
        form.addRow("移动平均 / 点", self.spn_filter)
        form.addRow("数学运算", self.cmb_math)
        form.addRow("输入 A / B", row(self.cmb_math_a, self.cmb_math_b))
        form.addRow("FFT / 分析源", self.cmb_analysis_src)
        self.lbl_processing = QLabel("处理在本地进行，原始归档保留设备采样值。")
        self.lbl_processing.setWordWrap(True)
        self.lbl_processing.setObjectName("muted")
        form.addRow(self.lbl_processing)
        analysis.addWidget(g)
        g = QGroupBox("FFT 频谱")
        form = QFormLayout(g)
        self.cmb_fft_window = combo([(x.title(), x) for x in ["hann", "hamming", "blackman", "rectangular"]])
        self.cmb_fft_window.currentIndexChanged.connect(self._analysis_options)
        self.cmb_fft_span = combo([("全部频带", 0), ("5 kHz", 5000), ("10 kHz", 10000), ("100 kHz", 100000), ("1 MHz", 1000000)])
        self.cmb_fft_span.currentIndexChanged.connect(self._analysis_options)
        form.addRow("窗函数", self.cmb_fft_window)
        form.addRow("显示频宽", self.cmb_fft_span)
        tip = QLabel("单边峰值幅度，单位 dBV；已校正窗增益，分析前去除直流分量。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        form.addRow(tip)
        analysis.addWidget(g)
        g = QGroupBox("参考波形 / 离线回放")
        form = QVBoxLayout(g)
        tip = QLabel("「保留参考」将当前分析波形以虚线叠加。打开 NPZ 归档后可离线缩放、滤波、测量及再次导出。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        form.addWidget(tip)
        open_button = QPushButton("打开波形归档")
        open_button.clicked.connect(self._open_archive)
        form.addWidget(open_button)
        analysis.addWidget(g)
        analysis.addStretch()
        return tabs

    def _set(self, widget, value):
        if isinstance(widget, NumericSpinBox) and (widget.editing or widget in self._pending_numeric):
            return
        if isinstance(widget, QComboBox) and widget.property("scope_scale_unit"):
            self._set_scale_readback(widget, value, widget.property("scope_scale_unit"))
            return
        was_blocked = widget.blockSignals(True)
        if isinstance(widget, QComboBox):
            index = widget.findData(value)
            if index < 0:
                index = widget.findText(str(value))
            if index < 0 and isinstance(value, (float, int)):
                index = min(range(widget.count()), key=lambda i: abs(widget.itemData(i) - value))
            if index >= 0:
                widget.setCurrentIndex(index)
        elif isinstance(widget, QCheckBox):
            widget.setChecked(value)
        else:
            widget.setValue(value)
        widget.blockSignals(was_blocked)

    def _set_scale_readback(self, widget, value, unit):
        """显示设备实际刻度；每个下拉框最多保留一个非预设读回项。"""
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value) or value <= 0:
            return
        readback_role = Qt.ItemDataRole.UserRole + 1
        readback_index = widget.findData(True, readback_role)
        standard_index = -1
        for index in range(widget.count()):
            preset = widget.itemData(index)
            if index != readback_index and isinstance(preset, (int, float)) and math.isclose(
                    preset, value, rel_tol=1e-9, abs_tol=0):
                standard_index = index
                break
        was_blocked = widget.blockSignals(True)
        try:
            if standard_index >= 0:
                if readback_index >= 0:
                    widget.removeItem(readback_index)
                    if readback_index < standard_index:
                        standard_index -= 1
                widget.setCurrentIndex(standard_index)
            else:
                # 读回项保留足够精度，避免把非预设刻度再四舍五入成邻近档。
                multiplier, prefix = 1e-12, "p"
                for candidate, candidate_prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"),
                                                     (1, ""), (1e-3, "m"), (1e-6, "µ"),
                                                     (1e-9, "n"), (1e-12, "p")):
                    if value >= candidate:
                        multiplier, prefix = candidate, candidate_prefix
                        break
                text = f"{value / multiplier:.9g} {prefix}{unit}/div（设备读回）"
                if readback_index < 0:
                    widget.addItem(text, value)
                    readback_index = widget.count() - 1
                    widget.setItemData(readback_index, True, readback_role)
                else:
                    widget.setItemText(readback_index, text)
                    widget.setItemData(readback_index, value)
                widget.setCurrentIndex(readback_index)
        finally:
            widget.blockSignals(was_blocked)

    def _set_step(self, widget, step):
        if not widget.editing and widget not in self._pending_numeric and widget.singleStep() != step:
            widget.setSingleStep(step)

    def _device_controls(self, connected):
        for widget in (self.btn_run, self.btn_single, self.btn_force, self.btn_auto, self.btn_disconnect, self.g_trigger):
            widget.setEnabled(connected)
        self.cmb_record_length.setEnabled(connected)
        for n, widgets in self._ch_widgets.items():
            widgets["group"].setEnabled(connected and n <= (self.session.adapter.channel_count if connected else 4))

    def _configure_channels(self, names):
        for n, widgets in self._ch_widgets.items():
            widgets["group"].setVisible(f"CH{n}" in names)
        for widget in (self.cmb_trig_src, self.cmb_math_a, self.cmb_math_b, self.cmb_analysis_src, self.cmb_cursor_source):
            previous = widget.currentText()
            widget.blockSignals(True)
            widget.clear()
            widget.addItems(names + (["MATH"] if widget is self.cmb_analysis_src else []))
            if previous in names:
                widget.setCurrentText(previous)
            elif widget is self.cmb_math_b and len(names) > 1:
                widget.setCurrentIndex(1)
            widget.blockSignals(False)
        self._analysis_options()

    def _show_source(self, source):
        self.lbl_source.setText(source)
        self.scope_view.data_source = source
        self.fft_view.data_source = source
        self.zoom_view.data_source = f"{source} · ZOOM"

    def _start_simulator_and_connect(self):
        self._do_connect({"method": 0, "host": "", "port": 0, "visa": "", "adapter": None})

    def _on_connect(self):
        dialog = ConnectDialog(self)
        if dialog.exec() == QDialog.Accepted:
            self._do_connect(dialog.selected())

    def _do_connect(self, options):
        if self._disconnecting:
            return
        if self.connector and self.connector.isRunning():
            return
        if not self._disconnect():
            return
        self.btn_connect.setEnabled(False)
        self.btn_demo.setEnabled(False)
        self.lbl_status.setText("正在连接设备…")
        self._connection_cancelled = False
        self.btn_disconnect.setEnabled(True)
        self.btn_disconnect.setText("取消连接")
        self.connector = ConnectionWorker(options, self)
        self.connector.connected.connect(self._connected)
        self.connector.failed.connect(self._connection_failed)
        self.connector.progress.connect(self.lbl_status.setText)
        self.connector.finished.connect(self._connection_finished)
        self.connector.start()
        self._connection_timer.start(15000)

    def _connection_finished(self):
        self._connection_timer.stop()
        if self._close_requested:
            QTimer.singleShot(0, self.close)

    def _cancel_connection_timeout(self):
        if self.connector and self.connector.isRunning():
            self._connection_cancelled = True
            self.connector.cancel()
            self.lbl_status.setText("连接超过 15 秒，正在取消；请检查 VISA 驱动、地址和设备远程接口")

    def _connected(self, result):
        self._connection_timer.stop()
        if self._connection_cancelled or self._close_requested:
            result[0].disconnect()
            if result[1]:
                result[1].stop()
            self._connection_failed("连接已取消")
            return
        self.session, self._sim = result
        self._raw_waveforms = {}
        self._measurement_cache = {}
        self._averaged_waveforms = {}
        self._settings = {}
        self._frame_settings = {}
        self.scope_view.waveforms = {}
        self.fft_view.waveforms = {}
        self.zoom_view.waveforms = {}
        self.scope_view.set_zoom_range(None)
        self.scope_view.update()
        self.fft_view.update()
        self._averager.clear()
        self._view_position = 0
        self._device_position = None
        self._frames = self._last_frame_at = 0
        self._reference(False)
        self.btn_reference.setChecked(False)
        adapter = self.session.adapter
        self.btn_disconnect.setText("断开")
        self._source = "模拟信号 · SIMULATION" if self._sim else f"真实设备 · {adapter.identity.model}"
        self._show_source(self._source)
        self.lbl_status.setText(adapter.identity.full)
        self._set(self.spn_filter, 1)
        self._set(self.cmb_average, 1)
        self._set(self.cmb_math, "off")
        self.btn_connect.setEnabled(True)
        self.btn_demo.setEnabled(True)
        self._device_controls(True)
        has_record_control = hasattr(adapter, "set_record_length")
        self.lbl_record_length.setVisible(has_record_control)
        self.cmb_record_length.setVisible(has_record_control)
        self._configure_channels([f"CH{n}" for n in range(1, adapter.channel_count + 1)])
        for widgets in self._ch_widgets.values():
            coupling = widgets["coupling"]
            coupling.blockSignals(True)
            coupling.clear()
            coupling.addItems(("DC", "AC", "GND") if self._sim else getattr(adapter, "supported_couplings", ("DC", "AC", "GND")))
            coupling.blockSignals(False)
        for widget, values, labels in (
            (self.cmb_trig_slope, getattr(adapter, "supported_trigger_slopes", ("RISE", "FALL")),
             {"RISE": "↗ 上升沿", "FALL": "↘ 下降沿", "BOTH": "↕ 双边沿", "EITHER": "↕ 双边沿"}),
            (self.cmb_trig_mode, getattr(adapter, "supported_trigger_modes", ("AUTO", "NORMAL", "SINGLE")),
             {"AUTO": "Auto · 自动", "NORMAL": "Normal · 正常", "SINGLE": "Single · 单次", "ALEVEL": "Auto Level · 自动电平"}),
        ):
            widget.blockSignals(True)
            widget.clear()
            for value in values:
                widget.addItem(labels[value], value)
            widget.blockSignals(False)
        from ..adapters.tektronix import TektronixAdapter
        self.lbl_record_mode.setVisible(isinstance(adapter, TektronixAdapter))
        self._set(self.spn_refresh, 150 if self._sim else self._real_refresh_delay)
        self.worker = AcquisitionWorker(adapter, interval_ms=self.spn_refresh.value(), settings_interval_ms=5000,
                                        initial_settings=getattr(self.session, "initial_settings", None),
                                        wait_for_display=True, parent=self)
        self.worker.dataReady.connect(self._on_data)
        self.worker.settingsReady.connect(self._on_settings)
        self.worker.stateChanged.connect(self._on_state)
        self.worker.commandFinished.connect(self._command_finished)
        self.worker.error.connect(self._acquisition_error)
        self.worker.waveformProgress.connect(self._waveform_progress)
        self.worker.start()

    def _refresh_delay_changed(self, value):
        if not self._sim:
            self._real_refresh_delay = value
        if self.worker:
            self.worker.set_interval(value)
        self._update_transfer_info()

    def _waveform_progress(self, progress):
        if self._disconnecting:
            return
        self.lbl_transfer_info.setText(
            f"{'RUN 实时读回' if progress.get('running') else 'STOP 完整记录'} · {progress['channel']} {progress['channel_points']:,} / {progress['record_points']:,} 点\n"
            f"全部通道 {progress['received_points']:,} / {progress['total_points']:,} 点 · 完成后更新波形")

    def _acquisition_error(self, message):
        self._show_source("采集异常 · 当前显示保留帧")
        self.statusBar().showMessage(f"设备通信：{message}", 10000)

    def _connection_failed(self, message):
        self._connection_timer.stop()
        self.btn_connect.setEnabled(True)
        self.btn_demo.setEnabled(True)
        self.btn_disconnect.setText("断开")
        self.btn_disconnect.setEnabled(False)
        self.lbl_status.setText(f"连接失败 · {message}")
        if not self._connection_cancelled and not self._close_requested:
            QMessageBox.warning(self, "连接失败", message)

    def _disconnect(self):
        if self._disconnecting:
            return False
        if self.connector and self.connector.isRunning():
            self._connection_cancelled = True
            self.connector.cancel()
            self.lbl_status.setText("正在取消连接…")
            return False
        if self.worker:
            self._disconnecting = True
            self.lbl_status.setText("正在完成当前通信并断开…")
            self.worker.request_stop()
            stopped = wait_thread_responsive(self.worker)
            if not stopped:
                # 仅超出正常 VISA 请求的等待期限后，才中止失去响应的驱动。
                self.worker.request_stop(abort_io=True)
                stopped = wait_thread_responsive(self.worker, timeout_ms=1500)
            self._disconnecting = False
            if not stopped:
                self.statusBar().showMessage("设备仍在完成通信，请稍后重试断开", 8000)
                return False
            self.worker.dataReady.disconnect(self._on_data)
            self.worker.settingsReady.disconnect(self._on_settings)
            self.worker.stateChanged.disconnect(self._on_state)
            self.worker.commandFinished.disconnect(self._command_finished)
            self.worker.error.disconnect(self._acquisition_error)
            self.worker.deleteLater()
            self.worker = None
        self.session.disconnect()
        if self._sim:
            self._sim.stop()
            self._sim = None
        self._shot_path = ""
        self._pending_numeric.clear()
        self._on_state("OFFLINE")
        self._device_controls(False)
        self.lbl_status.setText("设备已断开 · 当前波形可继续分析与保存")
        self._show_source(f"已断开 · 保留帧 · {self._source}")
        return True

    def closeEvent(self, event):
        if self._archive_writer and self._archive_writer.isRunning():
            self.statusBar().showMessage("正在完成完整记录保存…")
            if not wait_thread_responsive(self._archive_writer, timeout_ms=30000):
                event.ignore()
                return
        if self.connector and self.connector.isRunning():
            self._close_requested = True
            self._connection_cancelled = True
            self.connector.cancel()
            event.ignore()
            self.statusBar().showMessage("正在取消连接并关闭…", 5000)
            return
        if not self._disconnect():
            event.ignore()
            return
        super().closeEvent(event)

    def _submit(self, name, *args):
        if self.worker and self.session.connected:
            widget = (self._ch_widgets[args[0]]["offset"] if name == "set_channel_offset" else
                      self.spn_trig_level if name == "set_trigger_level" else None)
            if widget is not None:
                self._pending_numeric[widget] = (name, args)
            if name.startswith("set_") or name == "autoset":
                self._averager.clear()
            self.worker.submit(name, *args)

    def _on_state(self, state):
        self._state = state
        self.lbl_state.setText(state)
        self.btn_run.blockSignals(True)
        self.btn_run.setChecked(state == "RUN")
        self.btn_run.setText("■ 停止" if state in ("RUN", "ARMED") else "▶ 运行")
        self.btn_run.blockSignals(False)
        self.btn_single.setText("等待触发…" if state == "ARMED" else "Single · 单次")
        self.btn_single.setEnabled(self.session.connected and state != "ARMED")
        if state == "SINGLE DONE":
            self.statusBar().showMessage("单次触发完成 · 全部启用通道已冻结", 10000)

    def _command_finished(self, result):
        name = result["name"]
        for widget, request in list(self._pending_numeric.items()):
            if request == (name, tuple(result.get("args", ()))):
                del self._pending_numeric[widget]
        if "error" in result:
            self.statusBar().showMessage(result["error"], 10000)
            if name == "capture_screen":
                self._shot_path = ""
            return
        if name == "set_channel_enabled" and result["result"] is False:
            self.statusBar().showMessage("通道切换未生效，请在设备前面板恢复该通道", 10000)
        if name == "capture_screen" and self._shot_path:
            data, source = result["result"]
            from PySide6.QtGui import QImage
            image = QImage.fromData(data)
            path, self._shot_path = self._shot_path, ""
            if image.isNull() or not image.save(path):
                self.statusBar().showMessage("截图解码或保存失败", 8000)
            else:
                self.statusBar().showMessage(f"已保存 {source}：{path}", 8000)

    def _on_run_toggle(self):
        self._submit("stop" if self._state in ("RUN", "ARMED") else "run")

    def _on_single(self):
        self._averager.clear()
        self._submit("single")

    def _on_trigger_mode(self):
        if self.cmb_trig_mode.currentData() == "SINGLE":
            self._on_single()
        else:
            self._submit("set_trigger_mode", self.cmb_trig_mode.currentData())

    def _on_settings(self, settings):
        self._settings = settings
        position = settings["timebase"].position
        if self._device_position is None or position != self._device_position:
            self._view_position = position
            self._device_position = position
            self._set(self.spn_zoom_center, position)
        self._sync_controls(settings["channels"], settings["timebase"], settings["trigger"])

    def _sync_controls(self, channels, timebase, trigger):
        self._set_step(self.spn_offset, max(timebase.scale / 5, 1e-9))
        self._set_step(self.spn_trig_level, max(channels.get(trigger.source, ChannelSettings()).scale / 5, 1e-6))
        self._set(self.cmb_timebase, timebase.scale)
        if self.cmb_record_length.isVisible():
            if self.cmb_record_length.findData(timebase.record_length) < 0:
                self.cmb_record_length.blockSignals(True)
                self.cmb_record_length.addItem(f"{timebase.record_length:,} 点（设备读回）", timebase.record_length)
                self.cmb_record_length.blockSignals(False)
            self._set(self.cmb_record_length, timebase.record_length)
        self._set(self.spn_offset, self._view_position)
        self._set(self.cmb_trig_src, trigger.source)
        self._set(self.spn_trig_level, trigger.level)
        self._set(self.cmb_trig_mode, trigger.mode)
        if trigger.slope == "BOTH" and self.cmb_trig_slope.findData("BOTH") < 0:
            self.cmb_trig_slope.addItem("↕ 双边沿", "BOTH")
        self._set(self.cmb_trig_slope, trigger.slope)
        for name, ch in channels.items():
            widgets = self._ch_widgets.get(ch.number)
            if widgets:
                self._set_step(widgets["offset"], max(ch.scale / 5, 1e-6))
                if widgets["coupling"].findText(ch.coupling.value) < 0:
                    widgets["coupling"].blockSignals(True)
                    widgets["coupling"].addItem(ch.coupling.value)
                    widgets["coupling"].blockSignals(False)
                for key, value in [("enabled", ch.enabled), ("scale", ch.scale), ("offset", ch.offset), ("coupling", ch.coupling.value)]:
                    self._set(widgets[key], value)
        self.scope_view.timebase = replace(timebase, position=self._view_position)
        self.scope_view.channels, self.scope_view.trigger = channels, trigger
        self._update_bandwidth(channels)
        self.scope_view.update()
        self._update_zoom()

    def _on_data(self, payload):
        if self._disconnecting:
            if self.worker:
                self.worker.frame_consumed()
            return
        try:
            self._accept_frame(payload)
        finally:
            if self.worker:
                self.worker.frame_consumed()

    def _accept_frame(self, payload):
        self._show_source(self._source)
        self._raw_waveforms = {name: wf for name, wf in payload["waveforms"].items() if wf is not None}
        self._measurement_cache = {name: (wf, payload.get("measurements", {}).get(name))
                                   for name, wf in self._raw_waveforms.items()}
        self._settings = payload.get("settings") or self._settings
        self._frame_settings = {
            "channels": {name: replace(ch) for name, ch in self._settings.get("channels", {}).items()},
            "timebase": replace(self._settings.get("timebase", self.scope_view.timebase)),
            "trigger": replace(self._settings.get("trigger", self.scope_view.trigger)),
        }
        points = max((wf.points for wf in self._raw_waveforms.values()), default=0)
        self.spn_zoom_factor.blockSignals(True)
        self.spn_zoom_factor.setMaximum(max(100, points / 2))
        self.spn_zoom_factor.blockSignals(False)
        self._update_bandwidth(self._settings.get("channels", {}))
        count = 1 if self._state == "ARMED" else self.cmb_average.currentData()
        self._averaged_waveforms = self._averager.apply(self._raw_waveforms, count)
        self._render_processed()
        self._frames += 1
        now = time.monotonic()
        fps = 1 / max(0.001, now - self._last_frame_at) if self._last_frame_at else 0
        self._last_frame_at = now
        tb = self.scope_view.timebase
        points = max((wf.points for wf in self._raw_waveforms.values()), default=0)
        self.lbl_acquisition.setText(f"{_fmt_eng(tb.sample_rate, 'Sa/s')}  ·  {points:,} 点/通道  ·  {fps:.1f} 帧/s")
        self._last_acquisition_ms = payload.get("acquisition_ms", 0)
        self._update_transfer_info()
        self.statusBar().showMessage(f"已接收 {self._frames} 帧 · {len(self._raw_waveforms)} 通道 · 本轮 {payload.get('acquisition_ms', 0):.0f} ms")

    def _update_transfer_info(self):
        points = max((wf.points for wf in self._raw_waveforms.values()), default=0)
        tb = self._settings.get("timebase", self.scope_view.timebase)
        self.lbl_transfer_info.setText(f"设备记录 {tb.record_length:,} 点 · 本帧最多 {points:,} 点/通道\n"
                                      f"通信 {self._last_acquisition_ms:.0f} ms · 帧后延迟 {self.spn_refresh.value():.0f} ms\n"
                                      + ("RUN 实时读回 · 保存同次完整记录请手动 STOP / Single。"
                                         if any(wf.record_stable is False for wf in self._raw_waveforms.values())
                                         else "完整原始记录 · ZOOM 使用同一记录的真实采样点。"
                                         if self._raw_waveforms and all(wf.full_record is True for wf in self._raw_waveforms.values())
                                         else "完整性未经设备确认；NPZ 保存时校验深度及原生采样间隔。"))

    def _processing_options(self):
        return {"filter_samples": self.spn_filter.value(), "average": self.cmb_average.currentData(),
                "math": self.cmb_math.currentData(), "math_a": self.cmb_math_a.currentText(), "math_b": self.cmb_math_b.currentText(),
                "stacked": self.btn_stacked.isChecked(), "zoom_enabled": self.btn_zoom.isChecked(),
                "zoom_factor": self.spn_zoom_factor.value(), "zoom_center": self.spn_zoom_center.value(),
                "zoom_voltage_gain": self.cmb_zoom_voltage.currentData(),
                "cursor_enabled": self.btn_cursor.isChecked(), "cursor_mode": self.cmb_cursor_mode.currentData(),
                "cursor_source": self.cmb_cursor_source.currentText(), "cursor_all_channels": self.cmb_cursor_scope.currentData(),
                "cursor_times": list(self.scope_view.cursor_times), "cursor_voltages": list(self.scope_view.cursor_voltages)}

    def _processing_changed(self, *_):
        self._averager.clear()
        self._averaged_waveforms = dict(self._raw_waveforms)
        self._render_processed()

    def _render_processed(self):
        waveforms = {name: moving_average(wf, self.spn_filter.value()) for name, wf in self._averaged_waveforms.items()}
        channels = dict(self._settings.get("channels", {}))
        operation, warning = self.cmb_math.currentData(), ""
        if operation != "off":
            a, b = self.cmb_math_a.currentText(), self.cmb_math_b.currentText()
            if a in waveforms and b in waveforms:
                try:
                    wf = math_waveform(waveforms[a], waveforms[b], operation)
                    waveforms["MATH"] = wf
                    channels["MATH"] = ChannelSettings(number=5, scale=max(0.001, wf.vpp / 6), offset=wf.vmean)
                except ValueError as error:
                    warning = str(error)
            else:
                warning = "数学通道需要两个已启用且有数据的输入通道。"
        self.lbl_processing.setText(warning or "本地处理生效；原始归档保留设备采样值。")
        tb = replace(self._settings.get("timebase", self.scope_view.timebase), position=self._view_position)
        trigger = self._settings.get("trigger", TriggerSettings())
        self.scope_view.set_data(waveforms, channels, tb, trigger)
        self.fft_view.set_data(waveforms, channels, tb, trigger)
        previous = self.cmb_cursor_source.currentText()
        names = [name for name in waveforms if channels.get(name, ChannelSettings()).enabled]
        if names != [self.cmb_cursor_source.itemText(i) for i in range(self.cmb_cursor_source.count())]:
            self.cmb_cursor_source.blockSignals(True)
            self.cmb_cursor_source.clear()
            self.cmb_cursor_source.addItems(names)
            if previous in names:
                self.cmb_cursor_source.setCurrentText(previous)
            self.cmb_cursor_source.blockSignals(False)
        self._cursor_options()
        self._update_zoom()
        measurements = {}
        for name, wf in waveforms.items():
            cached_wf, cached_values = self._measurement_cache.get(name, (None, None))
            measurements[name] = cached_values if cached_wf is wf and cached_values is not None else measure(wf)
        self._update_measurements(measurements)

    def _analysis_options(self, *_):
        self.fft_view.fft_source = self.cmb_analysis_src.currentText()
        self.fft_view.fft_window = self.cmb_fft_window.currentData()
        self.fft_view.fft_span = self.cmb_fft_span.currentData()
        self.fft_view.update()
        self._cursor_readout(self.scope_view.cursor_values())

    def _toggle_fft(self, enabled):
        self.fft_view.setVisible(enabled)
        self._resize_plots()

    def _resize_plots(self):
        combined = self.btn_zoom.isChecked() and self.btn_fft.isChecked()
        self.scope_view.setMinimumHeight(150 if combined else 180)
        self.zoom_view.setMinimumHeight(170 if combined else 180)
        self.fft_view.setMinimumHeight(130 if combined else 140)
        self.setMinimumHeight(860 if combined else 780 if self.btn_zoom.isChecked() else 720)
        self.plots.setSizes([260 if self.btn_zoom.isChecked() else 480,
                             340 if self.btn_zoom.isChecked() else 0,
                             220 if self.btn_fft.isChecked() else 0])

    def _toggle_stacked(self, enabled):
        self.scope_view.set_stacked(enabled)
        self.zoom_view.set_stacked(enabled)

    def _toggle_zoom(self, enabled):
        self.zoom_view.setVisible(enabled)
        self.zoom_controls.setVisible(enabled)
        self.scope_view.setToolTip("拖动蓝色选区移动 ZOOM；拖动边框调节范围；在其他区域拖选。Alt+拖动平移总览。" if enabled else "滚轮调节设备时基；拖动平移视图。")
        self._update_zoom()
        self._resize_plots()
        self._cursor_readout(self.scope_view.cursor_values())

    def _reset_zoom(self):
        self._set(self.spn_zoom_factor, 4)
        self._set(self.spn_zoom_center, self.scope_view.timebase.position)
        self._set(self.cmb_zoom_voltage, 1)
        self._update_zoom()

    def _zoom_scale_changed(self, scale):
        if scale > 0:
            self.spn_zoom_factor.setValue(self.scope_view.timebase.scale / scale)

    def _zoom_range_changed(self, start, end):
        if end > start:
            span = self.scope_view.timebase.scale * self.scope_view.timebase.horizontal_divisions
            self._set(self.spn_zoom_center, (start + end) / 2)
            self._set(self.spn_zoom_factor, span / (end-start))
            self._update_zoom()

    def _update_zoom(self, *_):
        if self._zoom_syncing:
            return
        if not self.btn_zoom.isChecked():
            self.scope_view.set_zoom_range(None)
            return
        self._zoom_syncing = True
        try:
            tb = self.scope_view.timebase
            span = tb.scale * tb.horizontal_divisions
            width = span / self.spn_zoom_factor.value()
            center = min(tb.position + (span-width)/2, max(tb.position - (span-width)/2, self.spn_zoom_center.value()))
            self._set(self.spn_zoom_center, center)
            self._set_step(self.spn_zoom_center, max(width/10, 1e-9))
            selected = (center-width/2, center+width/2)
            self.scope_view.set_zoom_range(selected)
            gain = self.cmb_zoom_voltage.currentData()
            channels = {name: replace(ch, scale=ch.scale/gain) for name,ch in self.scope_view.channels.items()}
            zoom_tb = replace(tb, scale=width/tb.horizontal_divisions, position=center)
            self.zoom_view.set_data(self.scope_view.waveforms, channels, zoom_tb, self.scope_view.trigger)
            self.zoom_view.references = self.scope_view.references
            self.zoom_view.reference_channels = {name: replace(ch, scale=ch.scale/gain) for name,ch in self.scope_view.reference_channels.items()}
            counts = [last-first for wf in self.scope_view.waveforms.values()
                      for first, last in [sample_bounds(wf, *selected)]]
            points = min(counts, default=0)
            self.lbl_zoom_info.setText(f"{_fmt_eng(zoom_tb.scale, 's')}/div · {points} 个采样点" + (" · 点数不足" if points<2 else ""))
            self.zoom_view.update()
        finally:
            self._zoom_syncing = False

    def _toggle_cursors(self, enabled):
        self.cursor_controls.setVisible(enabled)
        self.scope_view.set_cursors_enabled(enabled)
        self.zoom_view.cursor_enabled = enabled
        self._sync_cursors(self.scope_view, self.zoom_view, self.scope_view.cursor_values())

    def _cursor_options(self, *_):
        source = self.cmb_cursor_source.currentText()
        for view in (self.scope_view, self.zoom_view):
            changed = view.cursor_source != source
            view.cursor_source = source
            view.cursor_mode = self.cmb_cursor_mode.currentData()
            view.cursor_all_channels = self.cmb_cursor_scope.currentData()
            if changed:
                view.reset_voltage_cursors()
            view.update()
        self._sync_cursors(self.scope_view, self.zoom_view, self.scope_view.cursor_values())

    def _sync_cursors(self, source, target, values):
        target.cursor_times = list(source.cursor_times)
        target.cursor_voltages = list(source.cursor_voltages)
        target.update()
        self._cursor_readout(values)

    def _update_bandwidth(self, channels):
        entries = []
        for name, ch in channels.items():
            if name == "MATH":
                continue
            label = "关闭（全带宽）" if ch.bandwidth == "FULL" else ch.bandwidth or "未知"
            entries.append(f"{name} {label}")
        self.lbl_bandwidth.setText("硬件带宽限制 · " + ("  |  ".join(entries) if entries else "等待设备设置"))

    def _reference(self, enabled):
        self.scope_view.references = dict(self.scope_view.waveforms) if enabled else {}
        self.scope_view.reference_channels = dict(self.scope_view.channels) if enabled else {}
        self.scope_view.update()
        self._update_zoom()

    def _cursor_readout(self, values):
        if not self.scope_view.cursor_enabled:
            self.lbl_cursor.setText("总览蓝框选择 ZOOM 区间 · 局部窗口滚轮放大、拖动平移 · 下方测量为整帧" if self.btn_zoom.isChecked() else "滚轮调节时基 · 拖动平移 · 开启游标后拖动 A / B 测量")
            return
        lines = []
        if self.scope_view.cursor_mode in ("time", "both"):
            frequency = _fmt_eng(values['frequency'], 'Hz') if values['dt'] else "—"
            lines.append(f"A {_fmt_eng(values['a'], 's')}  ·  B {_fmt_eng(values['b'], 's')}  |  Δt {_fmt_eng(values['dt'], 's')}  ·  1/Δt {frequency}")
            for name, readings in values["channels"].items():
                fmt = lambda key: _fmt_eng(readings[key], "V") if readings[key] is not None else "超出记录范围"
                lines.append(f"{name}：A {fmt('va')}  ·  B {fmt('vb')}  ·  ΔV {fmt('dv')}")
        if self.scope_view.cursor_mode in ("voltage", "both"):
            lines.append(f"水平光标 {self.scope_view.cursor_source}：C {_fmt_eng(values['c'], 'V')}  ·  D {_fmt_eng(values['d'], 'V')}  ·  ΔV {_fmt_eng(values['voltage_delta'], 'V')}")
        self.lbl_cursor.setText("\n".join(lines))

    def _update_measurements(self, measurements):
        self._last_measurements = measurements
        keys = ["Vpp", "Vrms", "Vavg", "Freq", "Period", "Duty", "Vmax", "Vmin"]
        self.measure_table.setRowCount(len(measurements))
        for r, (name, items) in enumerate(measurements.items()):
            values, texts = {m.name: m for m in items}, [name]
            for key in keys:
                m = values.get(key)
                texts.append(_fmt_eng(m.value, m.unit) if m and (key not in ("Freq", "Period", "Duty") or m.value != 0) else "—")
            for c, text in enumerate(texts):
                item = self.measure_table.item(r, c)
                if item is None:
                    item = QTableWidgetItem()
                    self.measure_table.setItem(r, c, item)
                item.setText(text)
                item.setTextAlignment(Qt.AlignCenter)
                if c == 0:
                    index = int(name[2:]) - 1 if name.startswith("CH") else 4
                    item.setForeground(QColor(CHANNEL_COLORS[index]))

    def _selected_channel(self):
        selected = self.measure_table.selectedItems()
        if selected:
            name = self.measure_table.item(selected[0].row(), 0).text()
            self.cmb_analysis_src.setCurrentText(name)
            self.cmb_cursor_source.setCurrentText(name)

    def _on_tb_combo(self):
        self._on_timebase_changed(self.cmb_timebase.currentData())

    def _on_timebase_changed(self, scale):
        self._set(self.cmb_timebase, scale)
        self.scope_view.timebase = replace(self.scope_view.timebase, scale=scale)
        self.scope_view.update()
        self._update_zoom()
        if self.session.connected:
            self._submit("set_timebase", scale)
        else:
            self._settings["timebase"] = replace(self._settings.get("timebase", self.scope_view.timebase), scale=scale)

    def _on_position_changed(self, position):
        self._view_position = position
        self._set(self.spn_offset, position)
        self.scope_view.timebase = replace(self.scope_view.timebase, position=position)
        self.scope_view.update()
        self._update_zoom()
        self._cursor_readout(self.scope_view.cursor_values())

    def _on_ch_scale(self, num):
        self._submit("set_channel_scale", num, self._ch_widgets[num]["scale"].currentData())

    def _on_ch_enabled(self, num, enabled):
        from ..adapters.rigol_ds1000e import RigolDs1000eAdapter
        if not enabled and isinstance(self.session.adapter, RigolDs1000eAdapter):
            answer = QMessageBox.question(self, "关闭设备通道", f"部分 DS1000E 固件关闭 CH{num} 后，需要在设备前面板重新开启。继续关闭？", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                self._set(self._ch_widgets[num]["enabled"], True)
                return
        self._submit("set_channel_enabled", num, enabled)

    def _export_waveforms(self):
        return {name: wf for name, wf in self.scope_view.waveforms.items() if wf.points >= 2}

    def _path(self, title, default, extension):
        path, _ = QFileDialog.getSaveFileName(self, title, default, f"{extension.upper()} (*.{extension})")
        if path and not path.lower().endswith(f".{extension}"):
            path += f".{extension}"
        return path

    def _save_archive(self):
        if self._archive_writer and self._archive_writer.isRunning():
            self.statusBar().showMessage("正在保存上一份完整记录，请等待完成", 6000)
            return
        if not self._raw_waveforms:
            self.statusBar().showMessage("当前没有波形可保存", 6000)
            return
        if any(wf.record_stable is False for wf in self._raw_waveforms.values()):
            QMessageBox.warning(self, "请先手动停止采集", "当前显示运行中的实时读回，各通道依次读取。请手动 STOP 或等待 Single 完成并收到完整记录，再保存 NPZ。软件不会自动切换 RUN / STOP。")
            return
        path = self._path("保存原始波形归档", f"scope_{datetime.now():%Y%m%d_%H%M%S}.npz", "npz")
        if path:
            try:
                identity = f"{self._source} · {self.session.adapter.identity.full}" if self.session.connected else self.lbl_status.text()
                settings = dict(self._frame_settings or self._settings)
                settings["timebase"] = replace(settings["timebase"], position=self._view_position)
                if max(wf.points for wf in self._raw_waveforms.values()) > 100000:
                    writer = ArchiveWriter(path, self._raw_waveforms, settings, identity, self._processing_options(), self)
                    self._archive_writer = writer
                    writer.saved.connect(lambda out: self.statusBar().showMessage(f"完整原始波形归档已保存：{out}", 10000))
                    writer.failed.connect(lambda error: QMessageBox.warning(self, "保存失败", error))
                    writer.finished.connect(lambda: self._archive_finished(writer))
                    self.statusBar().showMessage("正在后台保存完整原始记录…")
                    writer.start()
                else:
                    out = save_archive(path, self._raw_waveforms, settings, identity, self._processing_options(), require_complete=True)
                    self.statusBar().showMessage(f"原始波形归档已保存：{out}", 8000)
            except Exception as error:
                QMessageBox.warning(self, "保存失败", str(error))

    def _archive_finished(self, writer):
        if self._archive_writer is writer:
            self._archive_writer = None
        writer.deleteLater()

    def _open_archive(self):
        if self.connector and self.connector.isRunning():
            self.statusBar().showMessage("请等待设备连接完成后再打开归档", 5000)
            return
        path, _ = QFileDialog.getOpenFileName(self, "打开波形归档", "", "ScopeHost 归档 (*.npz)")
        if not path:
            return
        try:
            waveforms, settings, metadata = load_archive(path)
            if not self._disconnect():
                return
            self._settings, self._raw_waveforms = settings, waveforms
            self._measurement_cache = {}
            self.spn_zoom_factor.setMaximum(max(100, max(wf.points for wf in waveforms.values()) / 2))
            self._configure_channels(list(settings["channels"]))
            self._frame_settings = settings
            self._averaged_waveforms = dict(waveforms)
            self._averager.clear()
            self._view_position = settings["timebase"].position
            options = metadata.get("processing", {})
            self._set(self.spn_filter, options.get("filter_samples", 1))
            self._set(self.cmb_math, options.get("math", "off"))
            self._set(self.cmb_math_a, options.get("math_a", "CH1"))
            self._set(self.cmb_math_b, options.get("math_b", "CH2"))
            self._set(self.cmb_average, 1)
            self._on_settings(settings)
            self._render_processed()
            self._set(self.spn_zoom_factor, options.get("zoom_factor", 4))
            self._set(self.spn_zoom_center, options.get("zoom_center", self._view_position))
            self._set(self.cmb_zoom_voltage, options.get("zoom_voltage_gain", 1))
            self.btn_stacked.setChecked(options.get("stacked", False))
            self.btn_zoom.setChecked(options.get("zoom_enabled", False))
            self._update_zoom()
            self._set(self.cmb_cursor_mode, options.get("cursor_mode", "both"))
            self._set(self.cmb_cursor_source, options.get("cursor_source", self.cmb_cursor_source.currentText()))
            self._set(self.cmb_cursor_scope, options.get("cursor_all_channels", False))
            self._cursor_options()
            self.btn_cursor.setChecked(options.get("cursor_enabled", False))
            for key, attribute in (("cursor_times", "cursor_times"), ("cursor_voltages", "cursor_voltages")):
                values = options.get(key)
                if isinstance(values, list) and len(values) == 2 and all(isinstance(v, (int, float)) and np.isfinite(v) for v in values):
                    setattr(self.scope_view, attribute, list(values))
            self._sync_cursors(self.scope_view, self.zoom_view, self.scope_view.cursor_values())
            self._on_state("REPLAY")
            identity = metadata.get("identity", "")
            self._source = "离线回放 · 模拟信号" if "SIMULATION" in identity else "离线回放 · 真机采样" if "真实" in identity else "离线回放"
            self._show_source(self._source)
            self.lbl_status.setText(f"离线回放 · {Path(path).name} · {metadata.get('identity', '')}")
            points = max((wf.points for wf in waveforms.values()), default=0)
            self.lbl_acquisition.setText(f"{_fmt_eng(settings['timebase'].sample_rate, 'Sa/s')} · {points:,} 点/通道 · 离线回放")
            self._last_acquisition_ms = 0
            self._update_transfer_info()
            self.statusBar().showMessage("归档已打开 · 可离线分析；跨帧平均需实时采集", 10000)
        except Exception as error:
            QMessageBox.warning(self, "打开失败", str(error))

    def _export_csv(self, waveforms, default):
        from ..export import export_spreadsheet
        if not waveforms:
            self.statusBar().showMessage("当前没有可导出的波形", 6000)
            return
        path = self._path("导出波形 CSV", default, "csv")
        if path:
            try:
                export_spreadsheet(waveforms, path)
                self.statusBar().showMessage(f"已导出当前显示帧：{path}", 8000)
            except Exception as error:
                QMessageBox.warning(self, "导出失败", str(error))

    def _on_export_spreadsheet(self):
        self._export_csv(self._export_waveforms(), "scope_analysis.csv")

    def _zoom_waveforms(self):
        if not self.btn_zoom.isChecked() or self.scope_view.zoom_range is None:
            return {}
        start, end = self.scope_view.zoom_range
        result = {}
        for name, wf in self._export_waveforms().items():
            first, last = sample_bounds(wf, start, end)
            if last - first >= 2:
                result[name] = replace(wf, y=wf.y[first:last].copy(), x_start=wf.x_start + first * wf.x_incr,
                                       raw=wf.raw[first:last].copy() if wf.raw is not None else None,
                                       full_record=False)
        return result

    def _export_zoom(self):
        self._export_csv(self._zoom_waveforms(), "scope_zoom.csv")

    def _export_raw(self):
        self._export_csv(self._raw_waveforms, "scope_raw.csv")

    def _on_export(self):
        name = self.cmb_analysis_src.currentText()
        wf = self.scope_view.waveforms.get(name)
        self._export_csv({name: wf} if wf else {}, f"scope_{name.lower()}.csv")

    def _export_fft(self):
        wf = self.scope_view.waveforms.get(self.cmb_analysis_src.currentText())
        if not wf:
            self.statusBar().showMessage("所选 FFT 通道没有波形", 6000)
            return
        path = self._path("导出 FFT 频谱", "scope_fft.csv", "csv")
        if path:
            try:
                frequencies, magnitude = fft_spectrum(wf, self.cmb_fft_window.currentData())
                np.savetxt(path, np.column_stack((frequencies, magnitude)), delimiter=",", header="Frequency_Hz,Amplitude_dBV", comments="")
                self.statusBar().showMessage(f"频谱已导出：{path}", 8000)
            except Exception as error:
                QMessageBox.warning(self, "导出失败", str(error))

    def _export_measurements(self):
        if not self._last_measurements:
            return
        path = self._path("导出测量结果", "scope_measurements.csv", "csv")
        if path:
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as out:
                    writer = csv.writer(out)
                    writer.writerow(["Channel", "Measurement", "Value", "Unit"])
                    for name, items in self._last_measurements.items():
                        for item in items:
                            writer.writerow([name, item.name, item.value, item.unit])
                self.statusBar().showMessage(f"测量结果已导出：{path}", 8000)
            except OSError as error:
                QMessageBox.warning(self, "导出失败", str(error))

    def _on_export_ltspice(self):
        from ..export import export_ltspice_pwl, write_ltspice_asc
        waveforms = self._export_waveforms()
        if not waveforms:
            return
        directory = QFileDialog.getExistingDirectory(self, "选择 LTSpice 导出目录")
        if directory:
            try:
                files = export_ltspice_pwl(waveforms, directory)
                names = [Path(path).stem.replace("scope_", "").upper() for path in files]
                write_ltspice_asc(files, Path(directory) / "scope_ltspice.asc", names, max(wf.duration for wf in waveforms.values()))
                self.statusBar().showMessage(f"已导出 {len(files)} 个 PWL CSV 与 LTSpice 电路：{directory}", 10000)
            except Exception as error:
                QMessageBox.warning(self, "导出失败", str(error))

    def _save_plot(self):
        path = self._path("保存整个软件窗口", f"scope_window_{datetime.now():%Y%m%d_%H%M%S}.png", "png")
        if path:
            if self.grab().save(path):
                self.statusBar().showMessage(f"软件截图已保存：{path}", 8000)
            else:
                QMessageBox.warning(self, "保存失败", "无法写入图片")

    def _on_screenshot(self):
        if not self.session.connected:
            self._save_plot()
            return
        if self._shot_path:
            self.statusBar().showMessage("正在读取设备屏幕，请稍候", 5000)
            return
        path = self._path("保存设备屏幕图片", f"scope_device_{datetime.now():%Y%m%d_%H%M%S}.png", "png")
        if path:
            self._shot_path = path
            self._submit("capture_screen")
            self.statusBar().showMessage("正在后台读取设备屏幕…", 5000)

    def _on_screenshot_window(self):
        self._save_plot()

    def _help(self):
        QMessageBox.information(self, "ScopeHost 操作说明",
            "1. 连接真实设备；模拟信号演示会醒目标注 SIMULATION。空格运行 / 停止。\n"
            "2. 采集页设置时基及触发；Single 完成后冻结全部通道。\n"
            "3. 通道页调节刻度、偏移和 DC / AC / GND。\n"
            "4. 分析页设置数学、滤波与 FFT；测量表行可选择分析源。\n"
            "5. 垂直光标 A / B 显示时间及当前/全部通道电压；水平光标 C / D 测量所选通道的电压差。\n"
            "6. Ctrl+S 保存原始归档；Ctrl+O 打开；Ctrl+Shift+S 保存整个软件窗口截图。\n\n"
            "7. ZOOM 显示总览和局部窗口；拖动蓝框/边界调节范围。局部窗口滚轮放大、拖动平移、双击复位。\n"
            "8. 通道堆叠按通道分区并共用时间轴；保存/导出菜单可导出 ZOOM 区间。\n\n"
            "实时通道按设备协议顺序读取；Single 完成后读取冻结记录。\n"
            "本地测量精度受设备传输的采样点数与采样率限制。")
