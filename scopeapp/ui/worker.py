"""采集工作线程：波形快循环 + 设置慢循环分离，减少 SCPI 往返。"""
from __future__ import annotations

import time
import queue
import threading

from PySide6.QtCore import QThread, Signal

from ..adapters import ScopeAdapter
from ..measurements import measure


class _AcquisitionStopped(RuntimeError):
    """正常停止采集，在下一条 SCPI 指令前退出。"""


class AcquisitionWorker(QThread):
    """双频采集线程。

    - 帧后等待（interval_ms）：接收后，或 GUI 消费后，再开始等待。
    - 慢循环（settings_interval_ms，默认 2s）：刷新通道/时基/触发设置，
      并同步通道启用缓存（fetch_all 据此跳过禁用通道）。
    GUI 可选择单帧背压，避免排队积压；控制命令可打断等待。
    """

    dataReady = Signal(object)      # dict: {waveforms, measurements, settings}
    settingsReady = Signal(object)  # dict: {channels, timebase, trigger}
    error = Signal(str)
    stateChanged = Signal(str)
    commandFinished = Signal(object)
    waveformProgress = Signal(object)

    def __init__(
        self,
        adapter: ScopeAdapter,
        interval_ms: int = 400,
        settings_interval_ms: int = 2000,
        initial_settings=None,
        wait_for_display: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._adapter = adapter
        self._interval = interval_ms / 1000.0
        self._settings_interval = settings_interval_ms / 1000.0
        self._stop = False
        self._settings: dict = initial_settings or {}
        self._last_settings_at = 0.0
        self._commands = queue.Queue()
        self._wake = threading.Event()
        self._mode = "live"
        self._fetch_once = True
        self._refresh_requested = False
        self._wait_for_display = wait_for_display
        self._display_done = threading.Event()
        self._display_pending = False
        self._next_frame_at = 0.0

    def set_interval(self, interval_ms: int):
        self._interval = max(0.01, interval_ms / 1000)
        self._next_frame_at = time.monotonic() + self._interval
        self._wake.set()

    def frame_consumed(self):
        self._display_done.set()
        self._wake.set()

    def submit(self, name: str, *args) -> None:
        self._commands.put((name, args))
        self._wake.set()

    def request_stop(self, abort_io=False):
        self._stop = True
        self._wake.set()
        if abort_io:
            transport = getattr(getattr(self._adapter, "engine", None), "transport", None)
            if transport and hasattr(transport, "cancel"):
                transport.cancel()

    def stop(self) -> bool:
        self.request_stop()
        return self.wait(6000)

    def _execute_commands(self) -> None:
        while not self._stop:
            try:
                name, args = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if name == "capture_screen":
                    try:
                        result = (self._adapter.capture_screen(), "设备原生截图")
                    except NotImplementedError:
                        result = (self._adapter.render_screen_snapshot(), "设备数据重建")
                else:
                    result = getattr(self._adapter, name)(*args)
                    if name in ("run", "stop", "single"):
                        self._mode = {"run": "live", "stop": "stopped", "single": "single"}[name]
                        self._fetch_once = name != "single"
                        self.stateChanged.emit({"live": "RUN", "stopped": "STOP", "single": "ARMED"}[self._mode])
                    if name.startswith("set_") or name == "autoset":
                        self._refresh_requested = True
                        self._fetch_once = True
                self.commandFinished.emit({"name": name, "args": args, "result": result})
            except Exception as e:
                if self._stop:
                    return
                self.error.emit(f"{name}: {e}")
                self.commandFinished.emit({"name": name, "args": args, "error": str(e)})

    def _refresh_settings(self) -> None:
        channels = {}
        for n in range(1, self._adapter.channel_count + 1):
            if self._stop:
                return
            channels[f"CH{n}"] = self._adapter.read_channel(n)
        if self._stop:
            return
        timebase = self._adapter.read_timebase()
        trigger = self._adapter.read_trigger()
        self._adapter.update_enabled_cache(channels)
        self._settings = {"channels": channels, "timebase": timebase, "trigger": trigger}
        self._last_settings_at = time.monotonic()
        self._refresh_requested = False
        self.settingsReady.emit(self._settings)

    def run(self) -> None:
        # 完整接收正在执行的请求后，才在下一条指令前停止。直接中断
        # CURVE? 会把未读完的 USB 数据留给下一次连接。
        engine = getattr(self._adapter, "engine", None)
        original_guard = getattr(engine, "before_operation", None)

        def before_operation():
            if self._stop and not getattr(self._adapter, "_restoring_protocol", False):
                raise _AcquisitionStopped()
            if original_guard:
                original_guard()

        if engine is not None:
            engine.before_operation = before_operation
        previous_progress = getattr(self._adapter, "waveform_progress", None)
        previous_snapshot = getattr(self._adapter, "frame_settings_callback", None)
        if hasattr(self._adapter, "waveform_progress"):
            self._adapter.waveform_progress = self.waveformProgress.emit
            self._adapter.frame_settings_callback = self._refresh_settings
        try:
            self._acquire_loop()
        finally:
            if engine is not None and engine.before_operation is before_operation:
                engine.before_operation = original_guard
            if hasattr(self._adapter, "waveform_progress"):
                self._adapter.waveform_progress = previous_progress
                self._adapter.frame_settings_callback = previous_snapshot

    def _acquire_loop(self) -> None:
        # 首轮先取一次设置，保证第一次 dataReady 就带完整设置
        try:
            if self._settings:
                self._adapter.update_enabled_cache(self._settings["channels"])
                self._last_settings_at = time.monotonic()
                self.settingsReady.emit(self._settings)
            else:
                self._refresh_settings()
            self._mode = "live" if self._adapter.is_running() else "stopped"
            self.stateChanged.emit("RUN" if self._mode == "live" else "STOP")
        except Exception as e:  # noqa: BLE001
            if not self._stop:
                self.error.emit(str(e))

        while not self._stop:
            t0 = time.monotonic()
            try:
                self._execute_commands()
                if self._stop:
                    break
                if self._refresh_requested or time.monotonic() - self._last_settings_at >= self._settings_interval:
                    self._refresh_settings()
                    if self._mode != "single":
                        running = self._adapter.is_running()
                        mode = "live" if running else "stopped"
                        if mode != self._mode:
                            self._fetch_once = not running
                            self._mode = mode
                            self.stateChanged.emit("RUN" if running else "STOP")
                if self._stop:
                    break
                if self._display_pending:
                    if not self._display_done.is_set():
                        self._wake.wait(0.1)
                        self._wake.clear()
                        continue
                    self._display_pending = False
                    self._next_frame_at = time.monotonic() + self._interval
                remaining = self._next_frame_at - time.monotonic()
                if not self._fetch_once and self._mode == "live" and remaining > 0:
                    self._wake.wait(min(0.1, remaining))
                    self._wake.clear()
                    continue
                single_complete = self._mode == "single" and not self._adapter.is_running()
                if self._mode == "single" and not single_complete:
                    self._wake.wait(0.1)
                    self._wake.clear()
                    continue
                if self._mode == "stopped" and not self._fetch_once:
                    self._wake.wait(0.1)
                    self._wake.clear()
                    continue
                if getattr(self._adapter, "settings_before_waveform", False):
                    # DS1000E 没有波形前导参数，必须用同轮设置解码。
                    self._refresh_settings()
                waveforms = self._adapter.fetch_all()
                if self._stop:
                    break
                expected = {name for name, ch in self._settings.get("channels", {}).items() if ch.enabled}
                if expected and not waveforms:
                    raise RuntimeError("启用通道均未返回波形，请检查设备连接")
                if not expected.issubset(waveforms):
                    raise RuntimeError("未完整读取所有启用通道，保留上一帧并重试")
                for wf in waveforms.values():
                    if wf.full_record is True:
                        tb = self._settings["timebase"]
                        if wf.device_record_length != tb.record_length or not abs(wf.x_incr * tb.sample_rate - 1) < 1e-6:
                            raise RuntimeError("完整记录与本轮设备深度/采样率不符，保留上一帧并重试")
                measurements = {
                    name: measure(wf) for name, wf in waveforms.items() if wf is not None
                }
                if self._wait_for_display:
                    self._display_done.clear()
                    self._display_pending = True
                self.dataReady.emit({
                    "waveforms": waveforms,
                    "measurements": measurements,
                    "settings": self._settings,
                    "acquisition_ms": (time.monotonic() - t0) * 1000,
                })
                self._fetch_once = False
                if not self._wait_for_display:
                    self._next_frame_at = time.monotonic() + self._interval
                if single_complete:
                    self._mode = "stopped"
                    self.stateChanged.emit("SINGLE DONE")
            except Exception as e:  # noqa: BLE001 —— 单轮失败退避重试
                if self._stop:
                    break
                self.error.emit(str(e))
                self._wake.wait(max(self._interval, 1.0))
                self._wake.clear()
                continue
            self._wake.wait(0.01)
            self._wake.clear()
