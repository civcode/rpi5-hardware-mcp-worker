"""Offline serial tests using an in-memory UART; no hardware needed."""
import base64
import unittest
from unittest.mock import patch

from serial_core import (SerialWorker, SerialError, AT_MAX_TEXT_BYTES, AT_MAX_FRAME_BYTES,
                         AT_INTER_BYTE_SECONDS, _allowed_ports, serial_tool_result)


class FakeSerial:
    def __init__(self, reply=b"", pre=b""):
        self.port = None
        self.dtr = True
        self.rts = True
        self._reply = reply
        self._rx = bytearray(pre)
        self.writes = []
        self.closed = False
        self.opened = False
        self.wire_settings = None

    def open(self):
        self.opened = True
        self.wire_settings = (self.dtr, self.rts)

    @property
    def in_waiting(self):
        return len(self._rx)

    def read(self, n):
        if not self._rx:
            return b""
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def write(self, data):
        self.writes.append(data)
        # Replies are generated once the complete CRLF frame arrives, not
        # after each single byte of the paced transfer.
        if data.endswith(b"\n"):
            self._rx.extend(self._reply)
        return len(data)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class SerialCoreTests(unittest.TestCase):
    def make_worker(self, reply=b"\r\nOK\r\n", pre=b"", allow_writes=False):
        instances = []

        def factory(**kwargs):
            self.assertTrue(kwargs["exclusive"])
            self.assertFalse(kwargs["rtscts"])
            self.assertFalse(kwargs["xonxoff"])
            instance = FakeSerial(reply=reply, pre=pre)
            instances.append(instance)
            return instance

        return SerialWorker(ports=("/dev/ttyUSB0",), factory=factory,
                            lister=lambda: [], allow_writes=allow_writes), instances

    def test_reject_nonallowlisted_device_before_open(self):
        worker, instances = self.make_worker()
        with self.assertRaises(SerialError):
            worker.serial_capture(port="/dev/ttyACM0")
        with self.assertRaises(SerialError):
            worker.serial_at_query(command="AT", port="socket://evil")
        self.assertEqual(instances, [])

    def test_reject_bad_port_config(self):
        for value in ("", "../../tmp", "/dev/../etc/passwd", "/dev/ttyUSB0\n/dev/ttyUSB1"):
            with self.assertRaises(SerialError):
                _allowed_ports(value)

    def test_queries_are_strict_allowlist(self):
        worker, instances = self.make_worker()
        for s in ("AT+RESTORE", "AT+SAVE", "AT+SETCFG=1", "AT\r\nAT+RESET", "at", "AT+GETVER\n"):
            with self.assertRaises(SerialError):
                worker.serial_at_query(command=s)
        self.assertEqual(instances, [])

    def test_query_reads_raw_non_ascii_and_terminator(self):
        reply = b"\xff\x00tag:\x01\r\nOK\r\n"
        worker, instances = self.make_worker(reply=reply)
        result = worker.serial_at_query("AT+GETKLIST", quiet_ms=50, timeout_ms=200)
        self.assertEqual(result["rx_hex"], reply.hex())
        self.assertEqual(base64.b64decode(result["rx_base64"]), reply)
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["complete"])
        self.assertEqual(b"".join(instances[0].writes), b"AT+GETKLIST\r\n")
        self.assertTrue(all(len(chunk) == 1 for chunk in instances[0].writes))
        self.assertEqual(instances[0].wire_settings, (False, False))
        self.assertTrue(instances[0].closed)

    def test_split_terminal_crlf_is_preserved_before_close(self):
        class SplitReplySerial(FakeSerial):
            def read(self, n):
                result = super().read(n)
                if result.endswith(b"OK\r"):
                    self._rx.extend(b"\n")
                return result

        instances = []

        def factory(**kwargs):
            instance = SplitReplySerial(reply=b"version\r\nOK\r")
            instances.append(instance)
            return instance

        worker = SerialWorker(ports=("/dev/ttyUSB0",), factory=factory)
        result = worker.serial_at_query("AT+GETVER", quiet_ms=50, timeout_ms=500)
        self.assertEqual(bytes.fromhex(result["rx_hex"]), b"version\r\nOK\r\n")
        self.assertEqual(base64.b64decode(result["rx_base64"]), b"version\r\nOK\r\n")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(instances[0]._rx, b"")

    def test_chatter_prevents_query_and_retains_bytes(self):
        worker, instances = self.make_worker(pre=b"unsolicited\n")
        result = worker.serial_at_query("AT", quiet_ms=50)
        self.assertFalse(result["ok"])
        self.assertFalse(result["transmitted"])
        self.assertEqual(result["rx_hex"], b"unsolicited\n".hex())
        self.assertEqual(instances[0].writes, [])

    def test_no_terminator_is_incomplete_not_success(self):
        worker, _ = self.make_worker(reply=b"data-without-final-line")
        result = worker.serial_at_query("AT+GETVER", quiet_ms=50, timeout_ms=100)
        self.assertFalse(result["ok"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_capture_does_not_transmit(self):
        worker, instances = self.make_worker(pre=b"\x00\xfe\r\n")
        result = worker.serial_capture(duration_ms=50)
        self.assertFalse(result["transmitted"])
        self.assertEqual(result["rx_hex"], "00fe0d0a")
        self.assertFalse(instances[0].writes)

    def test_write_default_disabled_and_confirm_required(self):
        worker, instances = self.make_worker()
        payload = base64.b64encode(b"AT+RESTORE\r\n").decode()
        with self.assertRaises(SerialError):
            worker.serial_exchange(payload, confirm="SEND")
        self.assertEqual(instances, [])
        enabled, enabled_instances = self.make_worker(allow_writes=True)
        with self.assertRaises(SerialError):
            enabled.serial_exchange(payload)
        self.assertEqual(enabled_instances, [])
        result = enabled.serial_exchange(payload, confirm="SEND", timeout_ms=50)
        self.assertTrue(result["transmitted"])
        self.assertEqual(enabled_instances[0].writes, [b"AT+RESTORE\r\n"])

    def test_at_command_needs_no_confirmation_or_service_switch(self):
        # The stock setter and SAVE work with the default worker config.
        # This test uses fake UART only; it never writes STM32 flash.
        worker, instances = self.make_worker()
        for malformed in ("AT\rAT+RESTORE", "AT\n", "at+SAVE", "AT\x00", "XAT", "AT" + "a" * 160):
            with self.assertRaises(SerialError):
                worker.serial_at_command(malformed)
        self.assertEqual(instances, [])

        result = worker.serial_at_command("AT+PDOAOFF=37", timeout_ms=100)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(b"".join(instances[0].writes), b"AT+PDOAOFF=37\r\n")
        self.assertTrue(all(len(chunk) == 1 for chunk in instances[0].writes))

        saved = worker.serial_at_command("AT+SAVE", timeout_ms=100)
        self.assertEqual(saved["status"], "OK")
        self.assertEqual(b"".join(instances[1].writes), b"AT+SAVE\r\n")

    def test_at_command_signature_does_not_expose_confirmation(self):
        import inspect
        params = inspect.signature(SerialWorker.serial_at_command).parameters
        self.assertNotIn("confirm", params)

    def test_raw_exchange_remains_service_gated(self):
        worker, instances = self.make_worker()
        payload = base64.b64encode(b"AT+SAVE\r\n").decode("ascii")
        with self.assertRaisesRegex(SerialError, "disabled"):
            worker.serial_exchange(payload, confirm="SEND")
        self.assertEqual(instances, [])

    def test_at_frame_length_boundary_before_open(self):
        worker, instances = self.make_worker(allow_writes=True)
        valid = "AT" + "A" * (AT_MAX_TEXT_BYTES - 2)
        assert len(valid) == AT_MAX_TEXT_BYTES
        result = worker.serial_at_command(valid, timeout_ms=100)
        self.assertTrue(result["ok"])
        self.assertEqual(b"".join(instances[-1].writes), (valid + "\r\n").encode("ascii"))
        self.assertEqual(sum(map(len, instances[-1].writes)), AT_MAX_FRAME_BYTES)

        too_long = valid + "A"
        with self.assertRaisesRegex(SerialError, "61 bytes"):
            worker.serial_at_command(too_long)
        self.assertEqual(len(instances), 1, "oversized frame must be rejected before opening serial port")

    def test_historical_setdev_example_is_rejected_without_transmit(self):
        worker, instances = self.make_worker(allow_writes=True)
        command = "AT+SETDEV=2,0,0,0.000000,0.000000,1.000000,0.000000,1,795110505"
        self.assertGreater(len(command), AT_MAX_TEXT_BYTES)
        result = serial_tool_result(worker.serial_at_command, command)
        self.assertFalse(result["ok"])
        self.assertIn("61 bytes", result["error"])
        self.assertEqual(instances, [])

    def test_at_transmission_is_byte_paced_with_flush(self):
        from serial_core import SerialWorker
        class TrackingSerial(FakeSerial):
            def __init__(self):
                super().__init__()
                self.flush_count = 0
            def flush(self):
                self.flush_count += 1

        conn = TrackingSerial()
        with patch("serial_core.time.sleep") as pause:
            sent = SerialWorker._write_at_paced(conn, b"AT\r\n")
        self.assertEqual(sent, 4)
        self.assertEqual(conn.writes, [b"A", b"T", b"\r", b"\n"])
        self.assertEqual(conn.flush_count, 4)
        self.assertEqual(pause.call_count, 3)
        pause.assert_any_call(AT_INTER_BYTE_SECONDS)
        self.assertEqual([c.args for c in pause.call_args_list],
                         [(AT_INTER_BYTE_SECONDS,)] * 3)

    def test_terminal_error_is_not_success(self):
        worker, _ = self.make_worker(reply=b"\r\nERROR\r\n")
        result = worker.serial_at_query("AT", quiet_ms=50, timeout_ms=100)
        self.assertTrue(result["complete"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "ERROR")

    def test_oversized_write_and_bounds(self):
        worker, instances = self.make_worker(allow_writes=True)
        with self.assertRaises(SerialError):
            worker.serial_exchange(base64.b64encode(b"a" * 257).decode(), "SEND")
        with self.assertRaises(SerialError):
            worker.serial_capture(duration_ms=10001)
        with self.assertRaises(SerialError):
            worker.serial_capture(baudrate=True)
        self.assertEqual(instances, [])

    def test_mcp_error_result_preserves_expected_error_without_claiming_no_transmission(self):
        worker, instances = self.make_worker()
        result = serial_tool_result(worker.serial_at_query, "AT+SAVE")
        self.assertFalse(result["ok"])
        self.assertIn("allowlist", result["error"])
        self.assertEqual(result["error_type"], "SerialError")
        self.assertNotIn("transmitted", result)
        self.assertEqual(instances, [])

        def busy():
            raise OSError(16, "Device or resource busy")

        result = serial_tool_result(busy)
        self.assertFalse(result["ok"])
        self.assertIn("busy", result["error"])
        self.assertNotIn("transmitted", result)

    def test_list_exposes_only_configured(self):
        worker, _ = self.make_worker()
        result = worker.serial_list_ports()
        self.assertEqual([p["port"] for p in result["allowed"]], ["/dev/ttyUSB0"])


if __name__ == "__main__":
    unittest.main()
