"""Bounded GDB client running on the Pi beside the worker-managed OpenOCD.

No remote network access is required from the MCP caller.  Only predefined
GDB commands are used; no executable, command, or file path comes from MCP.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

MAX_MEMORY_BYTES = 4096
MAX_GDB_OUTPUT = 18000
FAULT_REGISTERS = {
    "ICSR": 0xE000ED04,
    "CCR": 0xE000ED14,
    "SHCSR": 0xE000ED24,
    "CFSR": 0xE000ED28,
    "HFSR": 0xE000ED2C,
    "DFSR": 0xE000ED30,
    "MMFAR": 0xE000ED34,
    "BFAR": 0xE000ED38,
}
CPU_REGISTER_RE = re.compile(
    r"^\s*(r(?:1[0-5]|[0-9])|sp|lr|pc|xpsr|msp|psp|primask|basepri|faultmask|control)\s+"
    r"(0x[0-9a-fA-F]+)\b", re.MULTILINE | re.IGNORECASE
)


class LocalGDB:
    def __init__(self, executable: str | None = None):
        self.executable = executable or os.getenv("RPI5_GDB", "gdb-multiarch")

    def _batch(self, port: int, commands: list[str]) -> dict:
        exe = shutil.which(self.executable)
        if not exe:
            return {"ok": False, "error": f"GDB executable not found: {self.executable}; install gdb-multiarch on edge"}
        # --nx/--nh exclude local init scripts. 'disconnect' does not resume
        # the target, unlike 'detach'. No host shell, user GDB command or ELF.
        args = [exe, "--nx", "--nh", "--quiet", "--batch",
                "-ex", "set confirm off", "-ex", "set pagination off",
                "-ex", "set remotetimeout 5",
                "-ex", f"target extended-remote 127.0.0.1:{port}"]
        for cmd in commands:
            args.extend(("-ex", cmd))
        args.extend(("-ex", "disconnect"))
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                    errors="replace", timeout=30, check=False,
                                    stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc)}
        output = (result.stdout + result.stderr)[-MAX_GDB_OUTPUT:]
        return {"ok": result.returncode == 0, "exit_code": result.returncode,
                "output": output}

    def inspect(self, port: int) -> dict:
        """Halt and read core registers, fault status, and nearby instructions."""
        commands = ["monitor halt", "info registers", "x/12i $pc"]
        commands.extend(f"x/1wx 0x{address:08x}" for address in FAULT_REGISTERS.values())
        result = self._batch(port, commands)
        if not result.get("ok"):
            return result
        output = result["output"]
        registers = {name.lower(): value.lower() for name, value in CPU_REGISTER_RE.findall(output)}
        system = {}
        for name, address in FAULT_REGISTERS.items():
            match = re.search(rf"(?:0x)?{address:08x}\s*:\s*(0x[0-9a-fA-F]+)", output, re.I)
            if match:
                system[name] = match.group(1).lower()
        result.update({"registers": registers, "system_registers": system,
                       "target_state": "halted", "resumed": False})
        if "pc" not in registers:
            result.update({"ok": False, "error": "GDB did not return a numeric PC register"})
        return result

    def read_memory(self, port: int, workspace: Path, address: int, length: int) -> dict:
        """Read a bounded target-memory range; file stays local and is deleted."""
        if type(address) is not int or not 0 <= address <= 0xFFFFFFFF:
            raise ValueError("address must be a 32-bit unsigned integer")
        if type(length) is not int or not 1 <= length <= MAX_MEMORY_BYTES:
            raise ValueError(f"length must be 1..{MAX_MEMORY_BYTES} bytes")
        if address + length > 0x100000000:
            raise ValueError("memory range wraps past 32-bit address space")
        with tempfile.TemporaryDirectory(prefix="gdb-read-", dir=workspace) as directory:
            output_path = Path(directory) / "target.bin"
            result = self._batch(port, ["monitor halt", f"dump binary memory {output_path} "
                                        f"0x{address:08x} 0x{address + length:08x}"])
            if not result.get("ok"):
                return result
            try:
                data = output_path.read_bytes()
            except OSError as exc:
                return {"ok": False, "error": f"GDB memory dump missing: {exc}", "output": result.get("output", "")}
            if len(data) != length:
                return {"ok": False, "error": f"GDB produced {len(data)} instead of {length} bytes"}
            return {"ok": True, "address": f"0x{address:08x}", "length": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "data_base64": base64.b64encode(data).decode("ascii"),
                    "data_hex": data.hex() if length <= 256 else None,
                    "target_state": "halted", "resumed": False}

    def control(self, port: int, action: str) -> dict:
        """Explicit MCU action. No reset, flash, or resume during inspection."""
        actions = {
            "halt": "monitor halt",
            "resume": "monitor resume",
            "reset_halt": "monitor reset halt",
        }
        if action not in actions:
            raise ValueError("action must be halt, resume, or reset_halt")
        result = self._batch(port, [actions[action]])
        result["action"] = action
        result["expected_target_state"] = "running" if action == "resume" else "halted"
        return result
