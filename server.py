"""Run the plain-LAN-HTTP OpenOCD and USB-UART MCP tools on the Raspberry Pi."""
from __future__ import annotations

import atexit
import logging
import os

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from worker_core import OpenOCDWorker, WorkerError
from firmware_upload import UploadError
from flash_snapshots import snapshot_tool_result
from worker_lifecycle import run_with_cleanup
from serial_core import SerialWorker, serial_tool_result
from artifacts import ArtifactStore, artifact_result
from datetime import datetime
from zoneinfo import ZoneInfo

class SessionTerminationTimestamp(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()

        if message == "Terminating session: None":
            timestamp = datetime.fromtimestamp(
                record.created,
                ZoneInfo("Europe/Vienna"),
            ).isoformat(timespec="milliseconds")

            record.msg = f"{timestamp} {message}"
            record.args = ()

        return True


logging.getLogger("mcp.server.streamable_http").addFilter(
    SessionTerminationTimestamp()
)

worker = OpenOCDWorker()
artifacts = ArtifactStore()
serial_worker = SerialWorker(artifacts=artifacts)
atexit.register(worker.close)


def _firmware_tool_result(fn, *args):
    """Return actionable MCP tool errors without an opaque UnexpectedToolError."""
    try:
        return fn(*args)
    except UploadError as exc:
        return exc.result()
    except WorkerError as exc:
        message = str(exc)
        if "Invalid base64" in message:
            code = "UPLOAD_BASE64_INVALID"
        elif "SHA-256" in message:
            code = "UPLOAD_SHA256_MISMATCH"
        else:
            code = "HEX_INVALID"
        return {"ok": False, "error": {"code": code, "message": message,
                "details": {"hint": "Check original HEX bytes, Base64 encoding and EOF record."}}}
    except OSError as exc:
        return {"ok": False, "error": {"code": "STAGING_IO_ERROR",
                "message": str(exc), "details": {"hint": "Check Pi staging workspace and available disk space."}}}

mcp = MCPServer(
    "rpi5-hardware",
    instructions=("Rapid experimentation with native GDB CLI/MI, OpenOCD and raw UART. "
                  "Choose operations using tool descriptions; SWD may halt and serial/programming commands may change state. "
                  "Debug Probe UART normally needs dtr=true. CH340 RTS may control NRST on BU04. "
                  "One OpenOCD process owns the probe at a time."),
)


@mcp.tool()
def diagnose_host() -> dict:
    """Host-only Linux/USB/OpenOCD diagnostics, Git revision/branch/dirty state and capabilities.
    Git metadata and file hashes reflect disk at query time, not proof of a restarted process."""
    return {**worker.diagnose_host(), "capabilities": {
        "gdb_cli": True, "gdb_mi": True, "serial_capture": True,
        "serial_modem_control": True, "raw_serial": True, "artifacts": True,
        "flash_snapshots": True, "flash_region_digests": True}}


@mcp.tool()
def inspect_openocd_host() -> dict:
    """Read fixed OpenOCD config candidates and executable hash; no target access."""
    return worker.inspect_openocd_host()



@mcp.tool()
def flash_snapshot(speed_khz: int = 1000, label: str = "", quirk: bool = False) -> dict:
    """Read 64/128 KiB STM32 main flash via SWD, HALT then explicitly RESUME; never flash or reset."""
    return snapshot_tool_result(worker.flash_snapshot, speed_khz, label, quirk)


@mcp.tool()
def flash_snapshot_list() -> dict:
    """List persistent snapshot IDs and SHA-256 hashes; host-only, no target access."""
    return snapshot_tool_result(worker.flash_snapshot_list)


@mcp.tool()
def flash_snapshot_info(snapshot_id: str) -> dict:
    """Rehash a saved snapshot and return size/path/metadata; host-only."""
    return snapshot_tool_result(worker.flash_snapshot_info, snapshot_id)


@mcp.tool()
def flash_snapshot_chunk(snapshot_id: str, offset: int = 0, length: int = 4096) -> dict:
    """Read at most 8192 bytes of a saved snapshot as Base64, hash-checked; host-only."""
    return snapshot_tool_result(worker.flash_snapshot_chunk, snapshot_id, offset, length)


@mcp.tool()
def flash_snapshot_diff(before_id: str, after_id: str, label: str = "") -> dict:
    """Offline compare saved flash images; store exhaustive byte CSV and range JSON; no target access."""
    return snapshot_tool_result(worker.flash_snapshot_diff, before_id, after_id, label)


@mcp.tool()
def flash_snapshot_diff_chunk(diff_id: str, format: str = "csv",
                              offset: int = 0, length: int = 4096) -> dict:
    """Fetch 1-8192 bytes of saved JSON/CSV diff over MCP, no target access."""
    return snapshot_tool_result(worker.flash_snapshot_diff_chunk, diff_id, format, offset, length)


@mcp.tool()
def probe_target(speed_khz: int = 100, quirk: bool = False) -> dict:
    """Connect via USB CMSIS-DAP SWD, HALT STM32, inspect ID and actual flash size; do not program."""
    return worker.probe_target(speed_khz, quirk)


@mcp.tool()
def fault_snapshot(speed_khz: int = 100, quirk: bool = False) -> dict:
    """HALT target, read registers and CFSR/HFSR/MMFAR/BFAR without resetting or flashing."""
    return worker.fault_snapshot(speed_khz, quirk)


@mcp.tool()
def start_gdb_server(speed_khz: int = 100, port: int = 3333, quirk: bool = False) -> dict:
    """Start OpenOCD on Raspberry Pi; external GDB on Oberon can connect to Pi:3333.
    No firmware files are transferred by this tool. Probe is owned by this server."""
    return worker.start_gdb_server(speed_khz, port, quirk)


@mcp.tool()
def gdb_session_start() -> dict:
    """Explicitly attach a persistent Pi-local GDB client to owned OpenOCD; may halt MCU.
    Required after session loss or explicit stop. Do not mix with an external GDB client."""
    return worker.gdb_session_start()


@mcp.tool()
def gdb_session_status() -> dict:
    """Host-only GDB identity, process health, and last observed target state; never attach."""
    return worker.gdb_session_status()


@mcp.tool()
def gdb_session_stop() -> dict:
    """Terminate only Pi-local GDB, leaving OpenOCD alive. No explicit halt/resume/reset;
    OpenOCD disconnect handling may affect target execution."""
    return worker.gdb_session_stop()


@mcp.tool()
def gdb_command(command: str, timeout_seconds: int = 30, session_id: str = "") -> dict:
    """Forward CLI or native MI to Pi-local GDB; empty command polls output.
    First call can attach; after loss/stop use gdb_session_start explicitly.
    Pass returned session_id to reject stale-session commands. File paths must
    exist on the Pi, not Oberon. Raw GDB commands may halt, reset or flash target."""
    return worker.gdb_command(command, timeout_seconds, session_id)


@mcp.tool()
def target_execution_status(refresh: bool = False) -> dict:
    """Report last observed execution state, or query monitor targets in an existing
    GDB session when refresh=true. Never attach, halt or resume to obtain status.
    A failed/unavailable observation is not proof of a running or halted MCU."""
    return worker.target_execution_status(refresh)


@mcp.tool()
def target_reset(mode: str, confirm: str = "", session_id: str = "") -> dict:
    """Convenience reset run/halt in an existing GDB session. Legacy confirm ignored.
    Returns acceptance separately from observed state. Reset may occur even if verification fails.
    Native monitor reset is an alternative. Never requests programming."""
    return worker.target_reset(mode, confirm, session_id)


@mcp.tool()
def firmware_fingerprint(snapshot_id: str = "", start_address: int = 0x08000000,
                         length: int = 0, expected_sha256: str = "",
                         capture_live: bool = False, speed_khz: int = 1000,
                         quirk: bool = False) -> dict:
    """SHA-256 over exact raw flash bytes; default application range ends at 0x08013800
    or factory capacity, whichever is lower. Use a saved snapshot OR capture_live=true.
    Live capture temporarily HALTS then RESUMES, and refuses if OpenOCD owns probe.
    Never programs/resets flash. Optional expected hash compares the same address interval.
    Returns exact range plus named raw full/application/config/protected digests from one snapshot;
    absent regions are null (e.g. config on 64 KiB). Not the HEX or ELF file hash."""
    return snapshot_tool_result(worker.firmware_fingerprint, snapshot_id, start_address,
                                length, expected_sha256, capture_live, speed_khz, quirk)


@mcp.tool()
def gdb_server_status() -> dict:
    """Check current OpenOCD GDB server state and return recent log lines."""
    return worker.server_status()


@mcp.tool()
def stop_gdb_server() -> dict:
    """Stop the OpenOCD GDB server; do not reset or flash the target."""
    return worker.stop_gdb_server()


@mcp.tool()
def stage_hex(base64_hex: str, expected_sha256: str = "") -> dict:
    """Legacy one-shot upload: base64_hex is the COMPLETE Base64 file contents,
    never a filename/path. Large MCP arguments may be truncated. Prefer chunk tools.
    Staging checks HEX and SHA-256 and does not access the target."""
    return _firmware_tool_result(worker.stage_hex, base64_hex, expected_sha256)


@mcp.tool()
def begin_firmware_upload(total_bytes: int, expected_sha256: str) -> dict:
    """Pi-host-only: start a bounded Intel HEX transfer.
    total_bytes is the ORIGINAL HEX FILE byte count; expected_sha256 its file hash.
    No target access, halt, reset or flash. Returns upload_id and max_chunk_bytes."""
    return _firmware_tool_result(worker.begin_firmware_upload, total_bytes, expected_sha256)


@mcp.tool()
def upload_firmware_chunk(upload_id: str, offset: int, chunk_base64: str) -> dict:
    """Pi-host-only: upload 1..8192 original file bytes encoded as Base64.
    offset is the zero-based BYTE offset in the original HEX file, NOT a record index.
    Send chunks sequentially; identical retries are safe. Returns next_offset."""
    return _firmware_tool_result(worker.upload_firmware_chunk, upload_id, offset, chunk_base64)


@mcp.tool()
def finalize_firmware_upload(upload_id: str) -> dict:
    """Pi-host-only: require exact file length, SHA-256, HEX checksums/EOF/range.
    Returns artifact_id usable by flash_preflight/flash_hex. DOES NOT flash or halt."""
    return _firmware_tool_result(worker.finalize_firmware_upload, upload_id)


@mcp.tool()
def abort_firmware_upload(upload_id: str) -> dict:
    """Pi-host-only: remove incomplete upload; does not access or modify target."""
    return _firmware_tool_result(worker.abort_firmware_upload, upload_id)


@mcp.tool()
def flash_preflight(artifact_id: str, allow_protected: bool = False) -> dict:
    """Host-only plan: verify staged HEX hash, report historical BU04 config overlap and probe ownership.
    allow_protected=true permits intentional config-region programming in the plan; pass the same
    option to flash_hex. Cached capacity is not a fresh probe. No target access or programming."""
    return _firmware_tool_result(worker.flash_preflight, artifact_id, allow_protected)


@mcp.tool()
def staged_hex_info(artifact_id: str) -> dict:
    """Rehash a staged HEX on the PI filesystem; the returned path is on the Pi,
    NOT Oberon. No target access; native GDB/OpenOCD may use the returned path."""
    return worker.staged_hex_info(artifact_id)


@mcp.tool()
def flash_hex(artifact_id: str, confirm_sha256: str, confirm: str = "", speed_khz: int = 100,
              quirk: bool = False, allow_protected: bool = False) -> dict:
    """Program staged Intel HEX: verifies expected file SHA-256, detects physical flash capacity,
    resets/halt, programs/verifies/resets MCU. Legacy confirm ignored. Historical BU04 config
    overlap blocked by default; allow_protected=true permits intentional overlap. Reports warning.
    Use native GDB/OpenOCD for other programming methods. Standalone probe requires sole ownership."""
    return worker.flash_hex(artifact_id, confirm_sha256, confirm, speed_khz, quirk, allow_protected)


@mcp.tool()
def serial_list_ports() -> dict:
    """Discover available and configured default UART ports, USB identities and adapter hints.
    No port is opened. Explicit /dev/ paths are accepted; prefer persistent by-id paths."""
    return serial_tool_result(serial_worker.serial_list_ports)


@mcp.tool()
def serial_capture(port: str = "", baudrate: int = 115200, duration_ms: int = 1000,
                   max_bytes: int = 4096, dtr: bool = False, rts: bool = False,
                   store: bool = False) -> dict:
    """Receive without transmitting. 8N1; duration 1..60000 ms, max_bytes 1..1048576.
    Empty port selects configured default. DTR/RTS are requested before open; drivers may glitch.
    Debug Probe 2e8a:000c normally requires dtr=true (no physical DTR pin).
    On BU04 CH340C, RTS assertion may hold NRST in reset. Agent selects settings.
    Example: port='/dev/ttyACM0', dtr=true, duration_ms=20000, max_bytes=262144, store=true.
    <=16 KiB returns inline raw views; larger data or store=true persists binary artifact.
    Use artifact_chunk and verify rx_sha256. elapsed_ms is receive window, offsets are host read
    times, truncated means byte cap reached. Zero bytes does not prove target silence."""
    return serial_tool_result(serial_worker.serial_capture, port, baudrate, duration_ms, max_bytes,
                              dtr, rts, store)


@mcp.tool()
def serial_at_query(command: str, port: str = "", baudrate: int = 115200,
                    timeout_ms: int = 2500, max_bytes: int = 4096,
                    quiet_ms: int = 150, dtr: bool = False, rts: bool = False) -> dict:
    """Stock AT convenience query: allowlisted AT/GET commands, <=61 ASCII bytes + CRLF,
    paced 5 ms/byte, quiet check before transmission, terminal OK/ERROR. Inspect raw response
    when chatty or incomplete. For arbitrary protocols use serial_exchange. See serial_capture
    for DTR/RTS and adapter behavior."""
    return serial_tool_result(serial_worker.serial_at_query, command, port, baudrate,
                              timeout_ms, max_bytes, quiet_ms, dtr, rts)


@mcp.tool()
def serial_at_command(command: str, port: str = "", baudrate: int = 115200,
                      timeout_ms: int = 2500, max_bytes: int = 4096,
                      dtr: bool = False, rts: bool = False) -> dict:
    """Stock AT convenience: <=61 ASCII bytes + CRLF, paced 5 ms/byte. Setters and SAVE
    can change settings, flash or reset. Terminal OK is not proof of target state. Use
    serial_exchange for arbitrary frames/pacing; DTR/RTS as in serial_capture."""
    return serial_tool_result(serial_worker.serial_at_command, command, port, baudrate,
                              timeout_ms, max_bytes, dtr, rts)


@mcp.tool()
def serial_exchange(tx_base64: str = "", confirm: str = "", port: str = "",
                    baudrate: int = 115200, timeout_ms: int = 1000, max_bytes: int = 4096,
                    dtr: bool = False, rts: bool = False, inter_byte_delay_ms: int = 0,
                    store: bool = False) -> dict:
    """General-purpose raw UART; no write flag or confirmation. Legacy confirm ignored.
    Base64 0..65536 bytes (empty = receive only), no appended CRLF. Optional pacing 0..1000 ms
    with total requested delay <=60 s. timeout_ms 1..60000 is RX window after TX.
    max_bytes 1..1048576. See serial_capture for modem lines, artifact and timing details.
    A failed write may have partially transmitted. Example tx_base64='QVQNCg==' sends AT CRLF."""
    return serial_tool_result(serial_worker.serial_exchange, tx_base64, confirm, port, baudrate,
                              timeout_ms, max_bytes, dtr, rts, inter_byte_delay_ms, store)


@mcp.tool()
def artifact_info(artifact_id: str) -> dict:
    """Host-only verify persistent artifact length/hash and metadata by returned ID."""
    return artifact_result(artifacts.info, artifact_id)


@mcp.tool()
def artifact_chunk(artifact_id: str, offset: int = 0, length: int = 8192) -> dict:
    """Retrieve verified 1..8192 binary bytes as Base64. Advance byte offset via next_offset,
    stop at eof, and verify full sha256 after concatenating decoded chunks."""
    return artifact_result(artifacts.chunk, artifact_id, offset, length)


@mcp.tool()
def artifact_list(limit: int = 100) -> dict:
    """List saved artifacts (1..1000 recent entries), no hardware access."""
    return artifact_result(artifacts.list, limit)


@mcp.tool()
def artifact_delete(artifact_id: str) -> dict:
    """Delete exactly the specified persistent artifact and its metadata, host-only."""
    return artifact_result(artifacts.delete, artifact_id)


if __name__ == "__main__":
    # Leave signal handling to Uvicorn. It drains the HTTP server on Ctrl+C;
    # then release worker-managed OpenOCD/resources explicitly before exit.
    run_with_cleanup(
        lambda: mcp.run(
            transport="streamable-http", host=os.getenv("RPI5_MCP_HOST", "0.0.0.0"),
            port=int(os.getenv("RPI5_MCP_PORT", "8766")),
            stateless_http=True, json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        ),
        worker.close,
    )
