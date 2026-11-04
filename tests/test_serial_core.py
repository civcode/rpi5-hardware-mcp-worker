"""Offline serial tests using an in-memory UART; no hardware needed."""
import base64
import unittest

from serial_core import SerialWorker, SerialError, _allowed_ports


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
        for s in ("AT+RESTORE", "AT+SETCFG=1", "AT\r\nAT+RESET", "at", "AT+GETVER\n"):
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
        self.assertEqual(instances[0].writes, [b"AT+GETKLIST\r\n"])
        self.assertEqual(instances[0].wire_settings, (False, False))
        self.assertTrue(instances[0].closed)

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

    def test_at_command_requires_opt_in_and_confirmation(self):
        worker, instances = self.make_worker()
        with self.assertRaises(SerialError):
            worker.serial_at_command("AT+RESTORE", "AT_WRITE")
        self.assertEqual(instances, [])
        enabled, open_instances = self.make_worker(allow_writes=True)
        with self.assertRaises(SerialError):
            enabled.serial_at_command("AT+RESTORE")
        for malformed in ("AT\rAT+RESTORE", "AT\n", "at+SAVE", "AT\x00", "XAT", "AT" + "a" * 160):
            with self.assertRaises(SerialError):
                enabled.serial_at_command(malformed, "AT_WRITE")
        self.assertEqual(open_instances, [])
        result = enabled.serial_at_command("AT+RESTORE", "AT_WRITE", timeout_ms=100)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(open_instances[0].writes, [b"AT+RESTORE\r\n"])

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

    def test_list_exposes_only_configured(self):
        worker, _ = self.make_worker()
        result = worker.serial_list_ports()
        self.assertEqual([p["port"] for p in result["allowed"]], ["/dev/ttyUSB0"])


if __name__ == "__main__":
    unittest.main()
