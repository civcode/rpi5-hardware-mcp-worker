"""Run a plain-HTTP MCP endpoint on the Raspberry Pi 5 USB debug host."""
from __future__ import annotations

import atexit
import os

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from worker_core import OpenOCDWorker

worker = OpenOCDWorker()
atexit.register(worker.close)

mcp = MCPServer(
    "rpi5-openocd",
    instructions=("OpenOCD SWD probe for STM32F103. Read-only operations halt the MCU; "
                  "flashing requires explicit confirmation. Use GDB on Oberon via TCP 3333 "
                  "for breakpoints/stepping/source-level debug."),
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


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http", host=os.getenv("RPI5_MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("RPI5_MCP_PORT", "8766")),
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
