"""Bounded USB-UART and AT command tools for a Raspberry Pi hardware worker."""
from __future__ import annotations

import base64
import binascii
import os
import re
import threading
import time
from typing import Callable


MAX_RX = 4096
MAX_TX = 256
# 2024 SDK: 64-byte receive array wraps when frame length reaches 64.
# Keep AT text <= 61 ASCII bytes so the appended CRLF fits within 63.
AT_MAX_TEXT_BYTES = 61
AT_MAX_FRAME_BYTES = AT_MAX_TEXT_BYTES + 2
# Earlier stock-firmware host implementation successfully paced one byte
# at a time with a 5 ms gap; not a proven minimum hardware requirement.
AT_INTER_BYTE_SECONDS = 0.005
# Give a split CRLF and immediately trailing UART bytes time to arrive.
TERMINAL_SETTLE_SECONDS = 0.1
READ_ONLY_AT = frozenset({
    "AT", "AT+GETVER", "AT+GETCFG", "AT+GETDEV", "AT+GETUWBMODE",
    "AT+PDOAGETCFG", "AT+GETDLIST", "AT+GETKLIST",
})
_TERMINAL = re.compile(rb"(?:^|[\r\n])(OK|ERROR)(?:\r\n|\r|\n)")


class SerialError(ValueError):
    pass


def serial_tool_result(operation: Callable, *args) -> dict:
    """Expose expected UART failures to MCP clients instead of generic errors.

    Transmission status is deliberately omitted on failure: an I/O error can
    occur after some bytes have already reached the device.
    """
    try:
        return operation(*args)
    except (SerialError, OSError) as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


def _integer(value: int, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise SerialError(f"{name} must be an integer between {low} and {high}")
    return value


def _allowed_ports(raw: str) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(s.strip() for s in raw.split(",") if s.strip()))
    if not result or len(result) > 8 or any(
        not s.startswith("/dev/") or ".." in s.split("/") or any(c in s for c in "\r\n\x00")
        for s in result
    ):
        raise SerialError("RPI5_SERIAL_PORTS must contain 1-8 absolute /dev/ paths")
    return result


class SerialWorker:
    """Serial access is serialized for the process and exclusive on POSIX.

    AT commands need per-call confirmation but no service-wide write switch.
    The legacy allow_writes flag applies only to unrestricted raw exchanges.
    Read-only query tools still use a strict command allowlist.
    """

    def __init__(self, ports: tuple[str, ...] | None = None, allow_writes: bool = False,
                 factory: Callable | None = None, lister: Callable | None = None):
        self.ports = ports if ports is not None else _allowed_ports(os.getenv("RPI5_SERIAL_PORTS", "/dev/ttyUSB0"))
        if not self.ports or tuple(dict.fromkeys(self.ports)) != _allowed_ports(",".join(self.ports)):
            raise SerialError("Invalid serial port configuration")
        self.allow_writes = allow_writes
        self._factory = factory
        self._lister = lister
        self._lock = threading.Lock()

    def _device(self, port: str) -> str:
        chosen = port or self.ports[0]
        if not isinstance(chosen, str) or chosen not in self.ports:
            raise SerialError("Serial port is not allowlisted")
        return chosen

    def _open(self, port: str, baudrate: int):
        _integer(baudrate, "baudrate", 1200, 921600)
        if self._factory is None:
            try:
                import serial
            except ImportError as exc:
                raise SerialError("Install pyserial from requirements.txt") from exc
            factory = serial.Serial
        else:
            factory = self._factory
        # Open only after line states are assigned; adapter drivers can still
        # generate a transient DTR/RTS edge. No modem line toggles are requested.
        conn = factory(port=None, baudrate=baudrate, bytesize=8, parity="N", stopbits=1,
                       timeout=0.05, write_timeout=1, xonxoff=False, rtscts=False,
                       dsrdtr=False, exclusive=True)
        conn.port = self._device(port)
        conn.dtr = False
        conn.rts = False
        conn.open()
        return conn

    @staticmethod
    def _at_frame(command: str) -> bytes:
        """Reject oversized/malformed AT requests before opening UART.

        The firmware's 64-byte frame accumulator wraps on byte 64.
        This limit includes CRLF, unlike the Python string's length.
        """
        if (not isinstance(command, str) or not command.startswith("AT") or
                not 2 <= len(command) <= AT_MAX_TEXT_BYTES or
                any(ord(c) < 32 or ord(c) > 126 for c in command)):
            raise SerialError(
                "AT command must be printable ASCII, start with AT, and use "
                f"at most {AT_MAX_TEXT_BYTES} bytes (plus CRLF; "
                f"{AT_MAX_FRAME_BYTES} bytes total)"
            )
        return (command + "\r\n").encode("ascii")

    @staticmethod
    def _write_at_paced(conn, frame: bytes) -> int:
        """Transmit one byte at a time with a >=5 ms inter-byte delay.

        The call to flush waits for the preceding byte to leave the adapter;
        this prevents a USB-serial write from arriving as an unpaced burst.
        """
        if not isinstance(frame, bytes) or not 4 <= len(frame) <= AT_MAX_FRAME_BYTES or not frame.endswith(b"\r\n"):
            raise SerialError("Invalid AT frame length or CRLF terminator")
        for i, byte in enumerate(frame):
            if conn.write(bytes((byte,))) != 1:
                raise SerialError("Short UART write; partial AT command may have been transmitted")
            conn.flush()
            if i + 1 < len(frame):
                time.sleep(AT_INTER_BYTE_SECONDS)
        return len(frame)

    @staticmethod
    def _read(conn, duration_ms: int, max_bytes: int, stop_at_terminal: bool = False) -> tuple[bytes, bool, bool]:
        deadline = time.monotonic() + duration_ms / 1000
        data = bytearray()
        terminal = False
        settle_deadline = None
        while time.monotonic() < deadline and len(data) < max_bytes:
            if settle_deadline is not None and time.monotonic() >= settle_deadline:
                break
            waiting = getattr(conn, "in_waiting", 0)
            piece = conn.read(min(max_bytes - len(data), max(1, waiting)))
            if not piece:
                time.sleep(0.002)
                continue
            data.extend(piece)
            if stop_at_terminal and (terminal or _TERMINAL.search(data)):
                terminal = True
                settle_deadline = time.monotonic() + TERMINAL_SETTLE_SECONDS
        return bytes(data), terminal, len(data) >= max_bytes

    @staticmethod
    def _result(data: bytes, port: str, baudrate: int, **fields) -> dict:
        return {
            "ok": True, "port": port, "baudrate": baudrate,
            "bytes_received": len(data), "rx_hex": data.hex(),
            "rx_base64": base64.b64encode(data).decode("ascii"),
            "rx_gb18030": data.decode("gb18030", errors="replace"),
            **fields,
        }

    def serial_list_ports(self) -> dict:
        if self._lister is None:
            try:
                from serial.tools import list_ports
            except ImportError as exc:
                raise SerialError("Install pyserial from requirements.txt") from exc
            available = list_ports.comports()
        else:
            available = self._lister()
        devices = {item.device: item for item in available}
        return {"allowed": [
            {"port": port, "present": port in devices or os.path.exists(port),
             "description": getattr(devices.get(port), "description", ""),
             "hwid": getattr(devices.get(port), "hwid", "")}
            for port in self.ports
        ]}

    def serial_capture(self, port: str = "", baudrate: int = 115200,
                       duration_ms: int = 1000, max_bytes: int = MAX_RX) -> dict:
        chosen = self._device(port)
        _integer(duration_ms, "duration_ms", 50, 10000)
        _integer(max_bytes, "max_bytes", 1, MAX_RX)
        with self._lock:
            conn = self._open(chosen, baudrate)
            try:
                rx, _, truncated = self._read(conn, duration_ms, max_bytes)
            finally:
                conn.close()
        return self._result(rx, chosen, baudrate, truncated=truncated, transmitted=False)

    def serial_at_query(self, command: str, port: str = "", baudrate: int = 115200,
                        timeout_ms: int = 2500, max_bytes: int = MAX_RX,
                        quiet_ms: int = 150) -> dict:
        chosen = self._device(port)
        if not isinstance(command, str) or command not in READ_ONLY_AT:
            raise SerialError("AT command is not on the read-only query allowlist")
        tx = self._at_frame(command)
        _integer(timeout_ms, "timeout_ms", 100, 10000)
        _integer(max_bytes, "max_bytes", 1, MAX_RX)
        _integer(quiet_ms, "quiet_ms", 50, 1000)
        with self._lock:
            conn = self._open(chosen, baudrate)
            try:
                pre, _, pre_truncated = self._read(conn, quiet_ms, min(512, max_bytes))
                if pre:
                    return {**self._result(pre, chosen, baudrate, transmitted=False,
                                           truncated=pre_truncated),
                            "ok": False, "error": "Unsolicited UART output during quiet check; query not sent"}
                self._write_at_paced(conn, tx)
                rx, terminal, truncated = self._read(conn, timeout_ms, max_bytes, True)
            finally:
                conn.close()
        hit = _TERMINAL.search(rx)
        status = hit.group(1).decode("ascii") if hit else "INCOMPLETE"
        return self._result(rx, chosen, baudrate, ok=status == "OK",
                            command=command, transmitted=True, terminal=terminal,
                            status=status, truncated=truncated, complete=terminal)

    def serial_exchange(self, tx_base64: str, confirm: str = "", port: str = "",
                        baudrate: int = 115200, timeout_ms: int = 1000,
                        max_bytes: int = MAX_RX) -> dict:
        """Opt-in state-changing raw exchange, never enabled by default."""
        if not self.allow_writes or confirm != "SEND":
            raise SerialError("State-changing serial exchanges disabled or missing confirm='SEND'")
        chosen = self._device(port)
        _integer(timeout_ms, "timeout_ms", 50, 10000)
        _integer(max_bytes, "max_bytes", 1, MAX_RX)
        if not isinstance(tx_base64, str) or len(tx_base64) > MAX_TX * 4 // 3 + 8:
            raise SerialError("Serial transmit payload is too large")
        try:
            tx = base64.b64decode(tx_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SerialError("tx_base64 must be valid Base64") from exc
        if not 1 <= len(tx) <= MAX_TX:
            raise SerialError("Serial transmit bytes must be between 1 and 256")
        with self._lock:
            conn = self._open(chosen, baudrate)
            try:
                written = conn.write(tx)
                if written != len(tx):
                    raise SerialError("Short UART write")
                conn.flush()
                rx, _, truncated = self._read(conn, timeout_ms, max_bytes)
            finally:
                conn.close()
        return self._result(rx, chosen, baudrate, transmitted=True,
                            tx_bytes=written, truncated=truncated)

    def serial_at_command(self, command: str, port: str = "",
                          baudrate: int = 115200, timeout_ms: int = 2500,
                          max_bytes: int = MAX_RX) -> dict:
        """Send an AT setter/save without a redundant confirmation argument.

        Still enforces printable ASCII, 61-character text limit, CRLF framing,
        and 5 ms inter-byte pacing. Raw UART exchange remains separately gated.
        """
        tx = self._at_frame(command)
        chosen = self._device(port)
        _integer(timeout_ms, "timeout_ms", 100, 10000)
        _integer(max_bytes, "max_bytes", 1, MAX_RX)
        with self._lock:
            conn = self._open(chosen, baudrate)
            try:
                self._write_at_paced(conn, tx)
                rx, terminal, truncated = self._read(conn, timeout_ms, max_bytes, True)
            finally:
                conn.close()
        hit = _TERMINAL.search(rx)
        status = hit.group(1).decode("ascii") if hit else "INCOMPLETE"
        return self._result(rx, chosen, baudrate, ok=status == "OK", command=command,
                            transmitted=True, terminal=terminal, status=status,
                            complete=terminal, truncated=truncated)
