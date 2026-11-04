"""Run the plain-LAN-HTTP OpenOCD and USB-UART MCP tools on the Raspberry Pi."""
from __future__ import annotations

import atexit
import os

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from worker_core import OpenOCDWorker
from serial_core import SerialWorker, serial_tool_result

worker = OpenOCDWorker()
serial_worker = SerialWorker(allow_writes=os.getenv("RPI5_SERIAL_ENABLE_WRITES") == "1")
atexit.register(worker.close)

mcp = MCPServer(
    "rpi5-hardware",
    instructions=("USB CMSIS-DAP/OpenOCD and UART/AT tools for STM32F103 BU03/BU04. "
                  "SWD probes halt the MCU; flashing requires explicit SHA-256 confirmation. "
                  "AT queries use a read-only command allowlist. Raw/setting UART writes "
                  "require RPI5_SERIAL_ENABLE_WRITES=1 and per-call confirmation."),
)


@mcp.tool()
def diagnose_host() -> dict:
    """Report Pi 5 Linux/USB/OpenOCD status; no target access or flash operations."""
    return worker.diagnose_host()


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
    """Send a recognized read-only AT command; return byte-exact response and status."""
    return serial_tool_result(serial_worker.serial_at_query, command, port, baudrate, timeout_ms, max_bytes, quiet_ms)


@mcp.tool()
def serial_at_command(command: str, confirm: str = "", port: str = "",
                      baudrate: int = 115200, timeout_ms: int = 2500,
                      max_bytes: int = 4096) -> dict:
    """Opt-in state-changing AT command; require confirm='AT_WRITE'."""
    return serial_tool_result(serial_worker.serial_at_command, command, confirm, port, baudrate, timeout_ms, max_bytes)


@mcp.tool()
def serial_exchange(tx_base64: str, confirm: str = "", port: str = "",
                    baudrate: int = 115200, timeout_ms: int = 1000,
                    max_bytes: int = 4096) -> dict:
    """Opt-in bounded raw UART transmit; require confirm='SEND'."""
    return serial_tool_result(serial_worker.serial_exchange, tx_base64, confirm, port, baudrate, timeout_ms, max_bytes)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http", host=os.getenv("RPI5_MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("RPI5_MCP_PORT", "8766")),
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
