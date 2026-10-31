"""真实 PyVISA ResourceManager API 合约与 USB/串口选择回归。"""
from __future__ import annotations
import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication, QDialog
from scopeapp.transport import scan_visa_resources, list_visa_resources, TransportError, VisaTransport
from scopeapp.ui.main_window import ConnectDialog, ConnectionWorker
from scopeapp.visa_process import VisaProcessTransport

USB = "USB0::0x1AB1::0x0588::DS1EI182307208::INSTR"
SERIAL = "ASRL3::INSTR"
APP = None


def report(resources):
    return {"resources": resources, "backend": "Visa Library at C:/Windows/System32/visa32.dll",
            "backend_requested": "auto", "pyvisa_version": "1.16.2", "python_bits": 64,
            "usb_backend_available": True}


class ManagerWithoutContextProtocol:
    """PyVISA 1.16.2 的 ResourceManager 只有 close，没有 __enter__/__exit__。"""
    visalib = "Visa Library at mock.dll"
    closed = False
    def list_resources(self):
        return (USB, SERIAL)
    def close(self):
        self.closed = True


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global APP
        APP = QApplication.instance() or QApplication([])

    def test_manager_without_context_protocol_scans_and_closes(self):
        manager = ManagerWithoutContextProtocol()
        with patch("pyvisa.ResourceManager", return_value=manager):
            actual = scan_visa_resources()
        self.assertEqual(actual["resources"], [USB, SERIAL])
        self.assertEqual(actual["usb_resources"], [USB])
        self.assertEqual(actual["serial_resources"], [SERIAL])
        self.assertEqual(actual["backend"], str(manager.visalib))
        self.assertTrue(manager.closed)

    def test_resource_listing_failures_are_visible_and_manager_is_closed(self):
        manager = ManagerWithoutContextProtocol()
        with patch("pyvisa.ResourceManager", return_value=manager), patch.object(manager, "list_resources", side_effect=RuntimeError("native enumerate failed")):
            with self.assertRaisesRegex(TransportError, "native enumerate failed"):
                list_visa_resources()
        self.assertTrue(manager.closed)

    def test_missing_vendor_library_cannot_be_silently_reported_as_no_devices(self):
        with patch("pyvisa.ResourceManager", side_effect=ValueError("Could not locate VISA library")):
            with self.assertRaisesRegex(TransportError, "Could not locate VISA library"):
                scan_visa_resources("@ivi")

    def test_explicit_backend_is_used_for_scan_and_connect(self):
        manager = ManagerWithoutContextProtocol()
        with patch("pyvisa.ResourceManager", return_value=manager) as factory:
            scan_visa_resources("@py")
            factory.assert_called_once_with("@py")
        class Instrument:
            def close(self): pass
        manager = ManagerWithoutContextProtocol()
        with patch.object(manager, "open_resource", return_value=Instrument(), create=True), patch("pyvisa.ResourceManager", return_value=manager) as factory:
            transport = VisaTransport(USB, backend_spec="@ivi")
            transport.connect()
            factory.assert_called_once_with("@ivi")
            transport.disconnect()
        self.assertTrue(manager.closed)

    def test_only_serial_resources_are_hidden_and_never_auto_selected(self):
        dialog = ConnectDialog()
        try:
            dialog._scan_finished(report([SERIAL, "ASRL1::INSTR"]))
            self.assertEqual(dialog.visa_res.count(), 0)
            self.assertEqual(dialog.selected()["visa"], "")
            self.assertIn("没有找到 USB", dialog.tip.text())
            self.assertIn("串口 2", dialog.backend_info.text())
            dialog.show_serial.setChecked(True)
            self.assertEqual(dialog.visa_res.count(), 2)
            self.assertEqual(dialog.selected()["visa"], "")
            self.assertFalse(dialog.visa_res.model().item(0).isEnabled())
        finally:
            dialog.close()

    def test_usb_is_preferred_and_serial_label_is_not_sent_to_device(self):
        dialog = ConnectDialog()
        try:
            dialog.show_serial.setChecked(True)
            dialog._scan_finished(report([SERIAL, USB]))
            self.assertEqual(dialog.selected()["visa"], USB)
            self.assertIn("USB 1", dialog.backend_info.text())
            dialog.visa_res.setEditText("ASRL3::INSTR")
            dialog._accept_selection()
            self.assertNotEqual(dialog.result(), QDialog.Accepted)
            self.assertIn("ASRL 是串口", dialog.tip.text())
        finally:
            dialog.close()

    def test_manual_usb_address_survives_scan_and_no_usb_report(self):
        dialog = ConnectDialog()
        try:
            manual = "USB0::vendor::product::manual_serial::INSTR"
            dialog.visa_res.setEditText(manual)
            dialog._scan_finished(report([SERIAL]))
            self.assertEqual(dialog.selected()["visa"], manual)
            dialog._accept_selection()
            self.assertEqual(dialog.result(), QDialog.Accepted)
        finally:
            dialog.close()

    def test_backend_error_is_distinct_from_empty_scan_and_can_be_exported(self):
        dialog = ConnectDialog()
        try:
            dialog._scan_failed("VISA 扫描失败：library missing")
            self.assertIn("library missing", dialog.tip.text())
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "diagnostic.json"
                with patch("scopeapp.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(path), "JSON")):
                    dialog._export_diagnostic()
                actual = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("library missing", actual["scan"]["error"])
            self.assertEqual(actual["selected"]["visa_backend"], "")
        finally:
            dialog.close()

    def test_backend_selected_in_ui_is_passed_to_connection_worker(self):
        dialog = ConnectDialog()
        try:
            dialog.visa_backend.setCurrentIndex(2)
            dialog.visa_res.setEditText(USB)
            options = dialog.selected()
            self.assertEqual(options["visa_backend"], "@py")
            worker = ConnectionWorker(options)
            with patch.object(worker.session, "connect_visa", side_effect=RuntimeError("stop before hardware")) as connect:
                worker.run()
                connect.assert_called_once_with(USB, None, "@py")
        finally:
            dialog.close()

    def test_asrl_is_rejected_before_opening_unrelated_com_port(self):
        worker = ConnectionWorker({"method": 2, "visa": SERIAL, "adapter": None})
        failures = []
        worker.failed.connect(failures.append)
        with patch.object(worker.session, "connect_visa") as connect:
            worker.run()
            connect.assert_not_called()
        self.assertTrue(failures)
        self.assertIn("ASRL 是串口", failures[0])


class WholeRecordTimeoutTests(unittest.TestCase):
    def test_large_binary_query_restores_timeout_and_termination(self):
        transport = VisaTransport(USB)
        instrument = SimpleNamespace(timeout=5000, read_termination="\n")
        transport._inst = instrument
        payload = b"#14\x01\n\r\x02"

        def read():
            self.assertEqual(instrument.timeout, 12000)
            self.assertIsNone(instrument.read_termination)
            return payload

        with patch.object(transport, "write") as write, patch.object(transport, "read_raw", side_effect=read):
            self.assertEqual(transport.ask_binary_with_timeout(":WAVFrm?", 12000), payload)
            write.assert_called_once_with(":WAVFrm?")
        self.assertEqual(instrument.timeout, 5000)
        self.assertEqual(instrument.read_termination, "\n")

    def test_failed_binary_query_restores_timeout_and_termination(self):
        transport = VisaTransport(USB)
        instrument = SimpleNamespace(timeout=5000, read_termination="\n")
        transport._inst = instrument
        with patch.object(transport, "write"), \
                patch.object(transport, "read_raw", side_effect=TransportError("device timeout")):
            with self.assertRaisesRegex(TransportError, "device timeout"):
                transport.ask_binary_with_timeout(":WAVFrm?", 12000)
        self.assertEqual(instrument.timeout, 5000)
        self.assertEqual(instrument.read_termination, "\n")

    def test_isolated_process_deadline_allows_the_native_query_timeout(self):
        transport = VisaProcessTransport()
        with patch.object(transport, "_rpc", return_value=b"native response") as rpc:
            self.assertEqual(transport.ask_binary_with_timeout(":WAVFrm?", 12000), b"native response")
            rpc.assert_called_once_with("ask_binary_with_timeout", ":WAVFrm?", 12000, deadline_ms=13500)


if __name__ == "__main__":
    mp.freeze_support()
    unittest.main(verbosity=2)
