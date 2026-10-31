"""正常断开完整接收当前 SCPI 响应，随后停止；全程使用假传输。"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from scopeapp.model import ChannelSettings, TimebaseSettings, TriggerSettings, Waveform
from scopeapp.scpi import ScpiEngine
from scopeapp.ui.main_window import MainWindow, wait_thread_responsive
from scopeapp.ui.worker import AcquisitionWorker

APP = QApplication.instance() or QApplication([])


def pump_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


class SlowTransport:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.commands = []
        self.completed = []
        self.cancel_count = 0
        self.closed = False

    def ask(self, command):
        self.commands.append(command)
        return "1"

    def write(self, command):
        self.commands.append(command)

    def ask_binary(self, command):
        self.commands.append(command)
        self.started.set()
        if not self.release.wait(2):
            raise RuntimeError("测试未释放假设备响应")
        self.completed.append(command)
        return b"\x00\x01\x02\x03"

    def cancel(self):
        self.cancel_count += 1
        self.release.set()

    def disconnect(self):
        self.closed = True


class SlowAdapter:
    channel_count = 2

    def __init__(self, engine):
        self.engine = engine

    def update_enabled_cache(self, _):
        pass

    def is_running(self):
        return bool(int(self.engine.ask("ACQUIRE:STATE?")))

    def fetch_all(self):
        result = {}
        for n in (1, 2):
            data = self.engine.ask_binary(f"CURVE{n}?")
            result[f"CH{n}"] = Waveform(f"CH{n}", np.frombuffer(data, dtype=np.uint8).astype(float))
        return result

    def set_channel_offset(self, *_):
        self.engine.ask_binary("SET?")
        self.engine.write("FOLLOWUP")


def initial_settings():
    return {
        "channels": {f"CH{n}": ChannelSettings(number=n) for n in (1, 2)},
        "timebase": TimebaseSettings(),
        "trigger": TriggerSettings(),
    }


class GracefulDisconnectTests(unittest.TestCase):
    def make_worker(self):
        transport = SlowTransport()
        engine = ScpiEngine(transport)
        original_guard = Mock()
        engine.before_operation = original_guard
        adapter = SlowAdapter(engine)
        worker = AcquisitionWorker(adapter, initial_settings=initial_settings(), settings_interval_ms=999999)
        errors, frames = [], []
        worker.error.connect(errors.append)
        worker.dataReady.connect(frames.append)
        return transport, engine, original_guard, adapter, worker, errors, frames

    def finish_worker(self, transport, worker):
        worker.request_stop()
        transport.release.set()
        self.assertTrue(wait_thread_responsive(worker, 2000))
        APP.processEvents()

    def test_stop_finishes_current_response_and_skips_next_channel(self):
        transport, engine, original_guard, _, worker, errors, frames = self.make_worker()
        worker.start()
        try:
            self.assertTrue(pump_until(transport.started.is_set))
            worker.request_stop()
            self.assertTrue(worker.isRunning(), "停止不能中断已经开始的响应")
            self.assertEqual(transport.cancel_count, 0)
            transport.release.set()
            self.assertTrue(wait_thread_responsive(worker, 1000))
            APP.processEvents()
            self.assertEqual(transport.completed, ["CURVE1?"])
            self.assertNotIn("CURVE2?", transport.commands)
            self.assertEqual((errors, frames), ([], []))
            self.assertIs(engine.before_operation, original_guard)
            calls = original_guard.call_count
            engine.ask("ACQUIRE:STATE?")
            self.assertEqual(original_guard.call_count, calls + 1, "原会话守卫必须恢复供后续操作使用")
        finally:
            self.finish_worker(transport, worker)

    def test_stop_during_queued_command_does_not_report_cancellation_error(self):
        transport, engine, original_guard, _, worker, errors, _ = self.make_worker()
        finished = []
        worker.commandFinished.connect(finished.append)
        worker.submit("set_channel_offset", 1, .25)
        worker.start()
        try:
            self.assertTrue(pump_until(transport.started.is_set))
            worker.request_stop()
            transport.release.set()
            self.assertTrue(wait_thread_responsive(worker, 1000))
            APP.processEvents()
            self.assertEqual(transport.completed, ["SET?"])
            self.assertNotIn("FOLLOWUP", transport.commands)
            self.assertEqual((errors, finished), ([], []))
            self.assertIs(engine.before_operation, original_guard)
        finally:
            self.finish_worker(transport, worker)

    def test_existing_session_guard_still_checks_operations(self):
        transport, engine, _, _, worker, errors, _ = self.make_worker()
        original_guard = Mock(side_effect=RuntimeError("已有会话已取消"))
        engine.before_operation = original_guard
        worker.start()
        try:
            self.assertTrue(pump_until(lambda: bool(errors)))
            self.assertIn("已有会话已取消", errors[0])
            self.assertEqual(transport.commands, [], "不能绕过原会话的取消守卫")
        finally:
            self.finish_worker(transport, worker)
        self.assertIs(engine.before_operation, original_guard)

    def test_window_disconnect_keeps_qt_responsive_until_reply_finishes(self):
        transport, engine, original_guard, adapter, worker, errors, _ = self.make_worker()
        engine._connected = True
        window = MainWindow()
        window.session.engine = engine
        window.session.transport = transport
        window.session.adapter = adapter
        window.worker = worker
        for signal, slot in ((worker.dataReady, window._on_data),
                             (worker.settingsReady, window._on_settings),
                             (worker.stateChanged, window._on_state),
                             (worker.commandFinished, window._command_finished),
                             (worker.error, window._acquisition_error)):
            signal.connect(slot)
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(20)
        worker.start()
        try:
            self.assertTrue(pump_until(transport.started.is_set))
            ticks.clear()
            release = threading.Timer(.25, transport.release.set)
            release.start()
            self.assertTrue(window._disconnect())
            release.join()
            self.assertGreaterEqual(len(ticks), 5)
            self.assertEqual(transport.cancel_count, 0)
            self.assertEqual(transport.completed, ["CURVE1?"])
            self.assertTrue(transport.closed)
            self.assertIsNone(window.worker)
            self.assertFalse(window.session.connected)
            self.assertIs(engine.before_operation, original_guard)
            self.assertEqual(errors, [])
        finally:
            timer.stop()
            if window.worker is not None:
                self.finish_worker(transport, worker)
            window.close()
            APP.processEvents()

    def test_window_only_forces_cancel_after_graceful_wait_expires(self):
        window = MainWindow()
        worker = Mock()
        window.worker = worker
        try:
            with patch("scopeapp.ui.main_window.wait_thread_responsive", side_effect=[False, True]) as wait:
                self.assertTrue(window._disconnect())
            self.assertEqual(worker.request_stop.call_args_list[0].args, ())
            self.assertEqual(worker.request_stop.call_args_list[0].kwargs, {})
            self.assertEqual(worker.request_stop.call_args_list[1].kwargs, {"abort_io": True})
            self.assertEqual(wait.call_args_list[1].kwargs, {"timeout_ms": 1500})
        finally:
            window.worker = None
            window.close()

    def test_adapter_without_engine_stops_normally(self):
        class Adapter:
            channel_count = 2
            update_enabled_cache = lambda *_: None
            is_running = lambda *_: True

            def fetch_all(self):
                return {f"CH{n}": Waveform(f"CH{n}", np.arange(4.0)) for n in (1, 2)}

        worker = AcquisitionWorker(Adapter(), initial_settings=initial_settings(), wait_for_display=True)
        frames = []
        worker.dataReady.connect(frames.append)
        worker.start()
        try:
            self.assertTrue(pump_until(lambda: bool(frames)))
            self.assertTrue(worker.stop())
        finally:
            worker.request_stop()
            self.assertTrue(wait_thread_responsive(worker, 1000))


if __name__ == "__main__":
    unittest.main(verbosity=2)
