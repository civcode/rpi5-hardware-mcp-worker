"""Run the plain-LAN-HTTP OpenOCD and USB-UART MCP tools on the Raspberry Pi."""
from __future__ import annotations

import atexit
import os

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from worker_core import OpenOCDWorker
from flash_snapshots import snapshot_tool_result
from worker_lifecycle import run_with_cleanup
from serial_core import SerialWorker, serial_tool_result

worker = OpenOCDWorker()
serial_worker = SerialWorker(allow_writes=os.getenv("RPI5_SERIAL_ENABLE_WRITES") == "1")  # Raw serial_exchange only
atexit.register(worker.close)

mcp = MCPServer(
    "rpi5-hardware",
    instructions=("USB CMSIS-DAP/OpenOCD and UART/AT tools for STM32F103 BU03/BU04. "
                  "SWD probes halt the MCU; flashing requires explicit SHA-256 confirmation. "
                  "AT queries use a read-only command allowlist. AT setters and "
                  "SAVE use validated, length-bounded, byte-paced UART without an unlock "
                  "or confirmation argument. Unrestricted raw UART still requires "
                  "RPI5_SERIAL_ENABLE_WRITES=1 and confirm='SEND'."),
)


@mcp.tool()
def diagnose_host() -> dict:
    """Report Pi 5 Linux/USB/OpenOCD status; no target access or flash operations."""
    return worker.diagnose_host()


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
    """Start OpenOCD background server; GDB on Oberon connects to rpi5:3333."""
    return worker.start_gdb_server(speed_khz, port, quirk)


@mcp.tool()
def gdb_command(command: str, auth_token: str, timeout_seconds: int = 30) -> dict:
    """Execute a GDB console or MI command in the authenticated local session."""
    return worker.gdb_command(command, auth_token, timeout_seconds)


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
    """Stage Intel HEX with checksum/range/SHA-256 checks; never program during staging."""
    return worker.stage_hex(base64_hex, expected_sha256)


@mcp.tool()
def staged_hex_info(artifact_id: str) -> dict:
    """Rehash a staged HEX and return its generated OpenOCD path; no target access."""
    return worker.staged_hex_info(artifact_id)


@mcp.tool()
def flash_hex(artifact_id: str, confirm_sha256: str, confirm: str = "", speed_khz: int = 100,
              quirk: bool = False) -> dict:
    """PROGRAM/verify staged HEX only with confirm='FLASH', verified hash, and detected flash capacity."""
    return worker.flash_hex(artifact_id, confirm_sha256, confirm, speed_khz, quirk)


@mcp.tool()
def serial_list_ports() -> dict:
    """List configured USB serial ports and presence/USB identifiers, no transmit."""
    return serial_tool_result(serial_worker.serial_list_ports)


@mcp.tool()
def serial_capture(port: str = "", baudrate: int = 115200,
                   duration_ms: int = 1000, max_bytes: int = 4096) -> dict:
    """Listen for UART bytes for a bounded interval, without transmitting bytes."""
    return serial_tool_result(serial_worker.serial_capture, port, baudrate, duration_ms, max_bytes)


@mcp.tool()
def serial_at_query(command: str, port: str = "", baudrate: int = 115200,
                    timeout_ms: int = 2500, max_bytes: int = 4096,
                    quiet_ms: int = 150) -> dict:
    """Send allowlisted AT query, <=61 ASCII bytes + CRLF, paced at 5 ms per byte."""
    return serial_tool_result(serial_worker.serial_at_query, command, port, baudrate, timeout_ms, max_bytes, quiet_ms)


@mcp.tool()
def serial_at_command(command: str, port: str = "",
                      baudrate: int = 115200, timeout_ms: int = 2500,
                      max_bytes: int = 4096) -> dict:
    """Send AT setter/save directly; <=61 ASCII bytes + CRLF, paced 5 ms/byte."""
    return serial_tool_result(serial_worker.serial_at_command, command, port, baudrate, timeout_ms, max_bytes)


@mcp.tool()
def serial_exchange(tx_base64: str, confirm: str = "", port: str = "",
                    baudrate: int = 115200, timeout_ms: int = 1000,
                    max_bytes: int = 4096) -> dict:
    """Opt-in bounded raw UART transmit; require confirm='SEND'."""
    return serial_tool_result(serial_worker.serial_exchange, tx_base64, confirm, port, baudrate, timeout_ms, max_bytes)


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
