"""Bounded USB-UART and AT command tools for a Raspberry Pi hardware worker."""
from __future__ import annotations

import base64
import hashlib
import binascii
import os
import re
import threading
import time
from typing import Callable

from artifacts import ArtifactStore, utc_now


MAX_RX = 4096
MAX_TX = 65536
MAX_CAPTURE = 1048576
MAX_DURATION = 60000
MAX_INLINE = 16384
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

    AT convenience tools preserve framing; raw exchange is general-purpose.
    Read-only query tools still use a strict command allowlist.
    """

    def __init__(self, ports: tuple[str, ...] | None = None, allow_writes: bool = False,
                 factory: Callable | None = None, lister: Callable | None = None,
                 artifacts: ArtifactStore | None = None):
        self.ports = ports if ports is not None else _allowed_ports(os.getenv("RPI5_SERIAL_PORTS", "/dev/ttyUSB0,/dev/ttyACM0"))
        if not self.ports or tuple(dict.fromkeys(self.ports)) != _allowed_ports(",".join(self.ports)):
            raise SerialError("Invalid serial port configuration")
        # Legacy allow_writes is accepted but no longer gates raw access.
        self.artifacts = artifacts if artifacts is not None else ArtifactStore()
        self._factory = factory
        self._lister = lister
        self._lock = threading.Lock()

    def _device(self, port: str) -> str:
        chosen = port or self.ports[0]
        if (not isinstance(chosen, str) or not chosen.startswith("/dev/")
                or ".." in chosen.split("/") or any(c in chosen for c in "\r\n\x00")):
            raise SerialError("port must be an absolute /dev/ device path")
        return chosen

    def _available(self):
        if self._lister is not None:
            return list(self._lister())
        try:
            from serial.tools import list_ports
        except ImportError as exc:
            if self._factory is not None:
                return []
            raise SerialError("Install pyserial from requirements.txt") from exc
        return list(list_ports.comports(include_links=True))

    def _identity(self, port, available=None):
        if available is None:
            try:
                available = self._available()
            except (OSError, SerialError):
                available = []
        info = next((x for x in available if x.device == port), None)
        if info is None:
            info = next((x for x in available if os.path.realpath(x.device) ==
                         os.path.realpath(port)), None)
        vid, pid = getattr(info, "vid", None), getattr(info, "pid", None)
        vidpid = f"{vid:04x}:{pid:04x}" if vid is not None and pid is not None else None
        result = {"usb_vidpid": vidpid, "usb_serial": getattr(info, "serial_number", None),
                  "usb_location": getattr(info, "location", None),
                  "description": getattr(info, "description", ""), "hwid": getattr(info, "hwid", "")}
        if vidpid == "2e8a:000c":
            result.update(requires_dtr_for_rx=True, recommended_dtr=True, recommended_rts=False)
        if vid == 0x1a86:
            result["warning"] = "On BU04, CH340C RTS# may hold NRST in reset; other wiring may differ."
        return result

    def _open(self, port: str, baudrate: int, dtr=False, rts=False):
        if type(dtr) is not bool or type(rts) is not bool:
            raise SerialError("dtr and rts must be booleans")
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
        # generate a transient DTR/RTS edge. Line states are requested before open.
        conn = factory(port=None, baudrate=baudrate, bytesize=8, parity="N", stopbits=1,
                       timeout=0.05, write_timeout=1, xonxoff=False, rtscts=False,
                       dsrdtr=False, exclusive=True)
        conn.port = self._device(port)
        conn.dtr = dtr
        conn.rts = rts
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
    def _read(conn, duration_ms: int, max_bytes: int, stop_at_terminal: bool = False,
              timing: dict | None = None) -> tuple[bytes, bool, bool]:
        start = time.monotonic()
        deadline = start + duration_ms / 1000
        first = last = None
        data = bytearray()
        terminal = False
        settle_deadline = None
        while time.monotonic() < deadline and len(data) < max_bytes:
            if settle_deadline is not None and time.monotonic() >= settle_deadline:
                break
            waiting = getattr(conn, "in_waiting", 0)
            if hasattr(conn, "timeout"):
                conn.timeout = min(0.05, max(0, min(deadline, settle_deadline or deadline) - time.monotonic()))
            piece = conn.read(min(max_bytes - len(data), max(1, waiting)))
            if not piece:
                time.sleep(0.002)
                continue
            last = int((time.monotonic() - start) * 1000)
            if first is None:
                first = last
            data.extend(piece)
            if stop_at_terminal and (terminal or _TERMINAL.search(data)):
                terminal = True
                settle_deadline = time.monotonic() + TERMINAL_SETTLE_SECONDS
        if timing is not None:
            timing.update(elapsed_ms=int((time.monotonic() - start) * 1000),
                          first_byte_offset_ms=first, last_byte_offset_ms=last,
                          ended="max_bytes" if len(data) >= max_bytes else
                          "terminal" if settle_deadline and time.monotonic() >= settle_deadline else "duration")
        return bytes(data), terminal, len(data) >= max_bytes

    def _result(self, data, port, baudrate, *, identity=None, dtr=False, rts=False,
                store=False, captured_utc=None, **fields):
        identity = identity if identity is not None else self._identity(port)
        result = {"ok": True, "port": port, "baudrate": baudrate, **identity,
                  "dtr": dtr, "rts": rts, "modem_lines": "requested, not electrical readback",
                  "captured_utc": captured_utc or utc_now(),
                  "bytes_received": len(data), "rx_sha256": hashlib.sha256(data).hexdigest(), **fields}
        if not data and identity.get("requires_dtr_for_rx") and not dtr:
            result["diagnostic_code"] = "DEBUG_PROBE_DTR_REQUIRED"
            result["hint"] = ("Debug Probe CDC forwarding normally requires DTR=true; "
                              "the official probe has no physical DTR output. Check firmware if still silent.")
        if store or len(data) > MAX_INLINE:
            try:
                result["artifact"] = self.artifacts.save(data, "serial_rx", result.copy())
            except (OSError, ValueError) as exc:
                result.update(ok=False, artifact_error=str(exc))
            result["inline_omitted"] = len(data) > MAX_INLINE
        if len(data) <= MAX_INLINE:
            result.update(rx_hex=data.hex(), rx_base64=base64.b64encode(data).decode("ascii"),
                          rx_gb18030=data.decode("gb18030", errors="replace"))
        return result

    def serial_list_ports(self):
        available = self._available()
        def item(port):
            return {"port": port, "present": os.path.exists(port) or any(
                x.device == port for x in available), **self._identity(port, available)}
        return {"allowed": [item(port) for port in self.ports],
                "available": [item(port) for port in sorted({x.device for x in available})],
                "note": "Configured ports are defaults, not an allowlist. Explicit /dev/ paths work."}

    def serial_capture(self, port="", baudrate=115200, duration_ms=1000, max_bytes=MAX_RX,
                       dtr=False, rts=False, store=False):
        chosen = self._device(port)
        _integer(duration_ms, "duration_ms", 1, MAX_DURATION)
        _integer(max_bytes, "max_bytes", 1, MAX_CAPTURE)
        timing = {}
        with self._lock:
            identity = self._identity(chosen)
            conn = self._open(chosen, baudrate, dtr, rts)
            try:
                captured = utc_now()
                rx, _, truncated = self._read(conn, duration_ms, max_bytes, timing=timing)
            finally:
                conn.close()
        return self._result(rx, chosen, baudrate, identity=identity, dtr=dtr, rts=rts,
                            store=store, captured_utc=captured, **timing,
                            truncated=truncated, transmitted=False)

    def serial_at_query(self, command: str, port: str = "", baudrate: int = 115200,
                        timeout_ms: int = 2500, max_bytes: int = MAX_RX,
                        quiet_ms: int = 150, dtr: bool = False, rts: bool = False) -> dict:
        chosen = self._device(port)
        if not isinstance(command, str) or command not in READ_ONLY_AT:
            raise SerialError("AT command is not on the read-only query allowlist")
        tx = self._at_frame(command)
        _integer(timeout_ms, "timeout_ms", 100, 10000)
        _integer(max_bytes, "max_bytes", 1, MAX_RX)
        _integer(quiet_ms, "quiet_ms", 50, 1000)
        timing = {}
        with self._lock:
            identity = self._identity(chosen)
            conn = self._open(chosen, baudrate, dtr, rts)
            try:
                captured = utc_now()
                pre, _, pre_truncated = self._read(conn, quiet_ms, min(512, max_bytes), timing=timing)
                if pre:
                    return {**self._result(pre, chosen, baudrate, identity=identity, dtr=dtr, rts=rts,
                                           captured_utc=captured, **timing, transmitted=False,
                                           truncated=pre_truncated),
                            "ok": False, "error": "Unsolicited UART output during quiet check; query not sent"}
                self._write_at_paced(conn, tx)
                rx, terminal, truncated = self._read(conn, timeout_ms, max_bytes, True, timing)
            finally:
                conn.close()
        hit = _TERMINAL.search(rx)
        status = hit.group(1).decode("ascii") if hit else "INCOMPLETE"
        return self._result(rx, chosen, baudrate, identity=identity, dtr=dtr, rts=rts,
                            captured_utc=captured, **timing, ok=status == "OK",
                            command=command, transmitted=True, terminal=terminal,
                            status=status, truncated=truncated, complete=terminal)

    def serial_exchange(self, tx_base64="", confirm="", port="", baudrate=115200,
                        timeout_ms=1000, max_bytes=MAX_RX, dtr=False, rts=False,
                        inter_byte_delay_ms=0, store=False):
        """Raw UART; legacy confirm is accepted but ignored."""
        chosen = self._device(port)
        _integer(timeout_ms, "timeout_ms", 1, MAX_DURATION)
        _integer(max_bytes, "max_bytes", 1, MAX_CAPTURE)
        _integer(inter_byte_delay_ms, "inter_byte_delay_ms", 0, 1000)
        if not isinstance(tx_base64, str) or len(tx_base64) > MAX_TX * 4 // 3 + 8:
            raise SerialError("Serial transmit payload is too large")
        try:
            tx = base64.b64decode(tx_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SerialError("tx_base64 must be valid Base64") from exc
        if len(tx) > MAX_TX:
            raise SerialError(f"Serial transmit bytes must be between 0 and {MAX_TX}")
        if max(0, len(tx)-1) * inter_byte_delay_ms > MAX_DURATION:
            raise SerialError("Requested pacing exceeds 60 seconds")
        timing = {}
        with self._lock:
            identity = self._identity(chosen)
            conn = self._open(chosen, baudrate, dtr, rts)
            try:
                written = 0
                if inter_byte_delay_ms:
                    started = time.monotonic()
                    for i, byte in enumerate(tx):
                        if conn.write(bytes((byte,))) != 1:
                            raise SerialError("Short UART write; partial transmission possible")
                        written += 1
                        conn.flush()
                        if i + 1 < len(tx):
                            time.sleep(inter_byte_delay_ms / 1000)
                        if time.monotonic() - started > MAX_DURATION / 1000:
                            raise SerialError("Paced write timed out; partial transmission possible")
                elif tx:
                    written = conn.write(tx)
                    if written != len(tx):
                        raise SerialError("Short UART write")
                    conn.flush()
                captured = utc_now()
                rx, _, truncated = self._read(conn, timeout_ms, max_bytes, timing=timing)
            finally:
                conn.close()
        return self._result(rx, chosen, baudrate, identity=identity, dtr=dtr, rts=rts,
                            store=store, captured_utc=captured, **timing,
                            transmitted=bool(written), tx_bytes=written,
                            tx_sha256=hashlib.sha256(tx).hexdigest(), truncated=truncated)

    def serial_at_command(self, command: str, port: str = "",
                          baudrate: int = 115200, timeout_ms: int = 2500,
                          max_bytes: int = MAX_RX, dtr: bool = False, rts: bool = False) -> dict:
        """Send an AT setter/save without a redundant confirmation argument.

        Still enforces printable ASCII, 61-character text limit, CRLF framing,
        and 5 ms inter-byte pacing. Raw UART exchange is general-purpose.
        """
        tx = self._at_frame(command)
        chosen = self._device(port)
        _integer(timeout_ms, "timeout_ms", 100, 10000)
        _integer(max_bytes, "max_bytes", 1, MAX_RX)
        timing = {}
        with self._lock:
            identity = self._identity(chosen)
            conn = self._open(chosen, baudrate, dtr, rts)
            try:
                captured = utc_now()
                self._write_at_paced(conn, tx)
                rx, terminal, truncated = self._read(conn, timeout_ms, max_bytes, True, timing)
            finally:
                conn.close()
        hit = _TERMINAL.search(rx)
        status = hit.group(1).decode("ascii") if hit else "INCOMPLETE"
        return self._result(rx, chosen, baudrate, identity=identity, dtr=dtr, rts=rts,
                            captured_utc=captured, **timing, ok=status == "OK", command=command,
                            transmitted=True, terminal=terminal, status=status,
                            complete=terminal, truncated=truncated)
