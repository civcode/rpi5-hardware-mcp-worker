"""OpenOCD/CMSIS-DAP orchestration for a Pi 5-hosted STM32F103 debugger.

No MCP dependency here.  All subprocesses run on the Raspberry Pi, not Oberon.
Probe and snapshot operations halt the STM32 but do not write its flash.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid

from flash_snapshots import FlashSnapshotStore


FLASH_START = 0x08000000
FLASH_LIMIT = FLASH_START + 128 * 1024
MAX_HEX_BYTES = 2 * 1024 * 1024
MAX_LOG_CHARS = 20_000
# Fixed allowlist: this host-only diagnostic never invokes OpenOCD's init.
OPENOCD_CONFIG_FILES = ("interface/cmsis-dap.cfg", "target/stm32f1x.cfg",
                        "target/swj-dp.tcl", "mem_helper.tcl")
OPENOCD_SCRIPT_ROOTS = ("/usr/local/share/openocd/scripts", "/usr/share/openocd/scripts")
MAX_DIAGNOSTIC_FILE_BYTES = 128 * 1024


class WorkerError(ValueError):
    pass


def validate_speed(khz: int) -> int:
    if type(khz) is not int or not 10 <= khz <= 5000:
        raise WorkerError("speed_khz must be an integer from 10 to 5000")
    return khz


def inspect_intel_hex(contents: bytes) -> dict:
    """Validate checksum, record layout and all data addresses; no device I/O."""
    if len(contents) > MAX_HEX_BYTES:
        raise WorkerError("HEX image is too large (2 MiB maximum)")
    try:
        lines = contents.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise WorkerError("HEX file must contain ASCII text") from exc
    upper = 0
    min_addr: int | None = None
    max_exclusive: int | None = None
    saw_eof = False
    total = 0
    for idx, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        if saw_eof:
            raise WorkerError(f"Unexpected record after EOF (line {idx})")
        if not line.startswith(":") or len(line) % 2 == 0:
            raise WorkerError(f"Invalid Intel HEX record at line {idx}")
        try:
            record = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise WorkerError(f"Invalid HEX characters at line {idx}") from exc
        if len(record) < 5 or len(record) != record[0] + 5 or sum(record) % 256:
            raise WorkerError(f"HEX length/checksum error on line {idx}")
        count, offset, kind = record[0], int.from_bytes(record[1:3], "big"), record[3]
        data = record[4:-1]
        if kind == 0x00:
            absolute = upper + offset
            end = absolute + count
            if count:
                if absolute < FLASH_START or end > FLASH_LIMIT:
                    raise WorkerError(f"HEX data address outside 128 KiB STM32 flash (line {idx})")
                min_addr = absolute if min_addr is None else min(min_addr, absolute)
                max_exclusive = end if max_exclusive is None else max(max_exclusive, end)
                total += count
        elif kind == 0x01:
            if count != 0 or offset != 0:
                raise WorkerError(f"Invalid EOF record (line {idx})")
            saw_eof = True
        elif kind == 0x02:
            if count != 2 or offset != 0:
                raise WorkerError(f"Invalid extended segment address (line {idx})")
            upper = int.from_bytes(data, "big") << 4
        elif kind == 0x04:
            if count != 2 or offset != 0:
                raise WorkerError(f"Invalid extended linear address (line {idx})")
            upper = int.from_bytes(data, "big") << 16
        elif kind in (0x03, 0x05):
            if count != 4 or offset != 0:
                raise WorkerError(f"Invalid start-address record (line {idx})")
        else:
            raise WorkerError(f"Unsupported record type {kind} (line {idx})")
    if not saw_eof or total <= 0 or min_addr is None or max_exclusive is None:
        raise WorkerError("HEX file has no data or is missing EOF")
    if min_addr != FLASH_START:
        raise WorkerError("HEX image must begin at STM32 flash base 0x08000000")
    return {
        "start": f"0x{min_addr:08x}", "end_exclusive": f"0x{max_exclusive:08x}",
        "max_flash_kib_needed": (max_exclusive - FLASH_START + 1023) // 1024,
        "data_bytes": total,
    }


def parse_flash_kib(probe_output: str) -> int | None:
    """Read STM32F103 factory flash-size half-word from mdh output."""
    # OpenOCD: '0x1ffff7e0: 0080' (hexadecimal 128 KiB)
    m = re.search(r"(?:0x)?1ffff7e0\s*:\s*(?:0x)?([0-9a-fA-F]{1,4})\b", probe_output, flags=re.I)
    if not m:
        return None
    capacity = int(m.group(1), 16)
    return capacity if 16 <= capacity <= 512 else None


class OpenOCDWorker:
    def __init__(self, openocd: str | None = None, staging_dir: str | None = None):
        self.openocd = openocd or os.getenv("OPENOCD", "openocd")
        self.workspace = Path(staging_dir) if staging_dir else Path(tempfile.mkdtemp(prefix="rpi5-openocd-"))
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._owns_workspace = staging_dir is None
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._log_handle = None
        self._port = 3333
        self._images: dict[str, dict] = {}
        self.flash_snapshots = FlashSnapshotStore()

    def close(self) -> None:
        self.stop_gdb_server()
        if self._owns_workspace:
            shutil.rmtree(self.workspace, ignore_errors=True)

    def _args(self, speed_khz: int, quirk: bool = False) -> list[str]:
        validate_speed(speed_khz)
        args = [self.openocd, "-f", "interface/cmsis-dap.cfg", "-c", "transport select swd"]
        if quirk:
            args += ["-c", "cmsis-dap quirk enable"]
        args += ["-f", "target/stm32f1x.cfg", "-c", f"adapter speed {speed_khz}"]
        return args

    def _server_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _one_shot(self, speed_khz: int, commands: list[str], timeout: int, quirk: bool = False) -> dict:
        with self._lock:
            if self._server_running():
                return {"ok": False, "error": "GDB server owns the probe. Stop it or use GDB monitor commands."}
            cmd = self._args(speed_khz, quirk)
            cmd += ["-c", "gdb_port disabled", "-c", "telnet_port disabled", "-c", "tcl_port disabled"]
            for c in commands:
                cmd += ["-c", c]
            try:
                r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", timeout=timeout, check=False)
                return {"ok": r.returncode == 0, "exit_code": r.returncode,
                        "output": r.stdout[-MAX_LOG_CHARS:], "command": cmd}
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"ok": False, "error": str(exc), "command": cmd}

    def diagnose_host(self) -> dict:
        commands = {
            "uname": ["uname", "-a"], "usb_probe": ["lsusb", "-d", "2e8a:000c"],
            "usb_topology": ["lsusb", "-t"], "openocd_version": [self.openocd, "--version"],
        }
        results = {}
        for key, command in commands.items():
            try:
                p = subprocess.run(command, capture_output=True, text=True,
                                   errors="replace", timeout=8, check=False)
                results[key] = {"exit_code": p.returncode, "output": (p.stdout + p.stderr)[-3000:]}
            except (OSError, subprocess.TimeoutExpired) as exc:
                results[key] = {"error": str(exc)}
        return {"host": socket.gethostname(), "tools": results,
                "hint": "Official Debug Probe USB ID is 2e8a:000c; check firmware with lsusb -v."}

    def inspect_openocd_host(self) -> dict:
        """Read fixed config candidates and binary metadata; no OpenOCD target access.

        Results are candidates, not proof of OpenOCD's effective search path,
        compiled-in Tcl or the exact source used to build the executable.
        """
        executable = shutil.which(self.openocd)
        result = {"configured_executable": self.openocd, "resolved_executable": executable,
                  "openocd_scripts_env": os.getenv("OPENOCD_SCRIPTS"), "scripts": [],
                  "note": "Candidate files only; effective search order and embedded Tcl are not proven."}
        if executable:
            try:
                digest = hashlib.sha256()
                with open(executable, "rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                result["executable_sha256"] = digest.hexdigest()
                version = subprocess.run([executable, "--version"], capture_output=True,
                                         text=True, errors="replace", timeout=8, check=False)
                result["version"] = {"exit_code": version.returncode,
                                     "output": (version.stdout + version.stderr)[-3000:]}
            except (OSError, subprocess.TimeoutExpired) as exc:
                result["executable_error"] = str(exc)
        roots = list(OPENOCD_SCRIPT_ROOTS)
        env_root = os.getenv("OPENOCD_SCRIPTS")
        if env_root:
            roots.insert(0, env_root)
        for root in dict.fromkeys(roots):
            for relative in OPENOCD_CONFIG_FILES:
                path = Path(root) / relative
                try:
                    with path.open("rb") as stream:
                        data = stream.read(MAX_DIAGNOSTIC_FILE_BYTES + 1)
                    if len(data) > MAX_DIAGNOSTIC_FILE_BYTES:
                        result["scripts"].append({"path": str(path), "error": "file exceeds size limit"})
                        continue
                    result["scripts"].append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
                                              "content": data.decode("utf-8", errors="replace")})
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    result["scripts"].append({"path": str(path), "error": str(exc)})
        return result


    def flash_snapshot(self, speed_khz: int = 1000, label: str = "",
                       quirk: bool = False) -> dict:
        """Dump detected 64/128 KiB main flash without programming or resetting.

        Temporarily halts MCU, then attempts explicit resume even on read error.
        Never shares the probe with a worker-managed OpenOCD GDB server.
        """
        validate_speed(speed_khz)
        with self._lock:
            if self._server_running():
                return {"ok": False, "error": "GDB server owns the probe. Stop it before taking a snapshot."}
            return self.flash_snapshots.capture(self._args(speed_khz, quirk), self.workspace, label)

    def flash_snapshot_info(self, snapshot_id: str) -> dict:
        """Check persistent snapshot hash and metadata; host-only."""
        return self.flash_snapshots.info(snapshot_id)

    def flash_snapshot_list(self) -> dict:
        """List persistent snapshot IDs, sizes and hashes; host-only."""
        return self.flash_snapshots.list_snapshots()

    def flash_snapshot_chunk(self, snapshot_id: str, offset: int = 0,
                             length: int = 4096) -> dict:
        """Return a hash-checked, bounded Base64 segment; host-only."""
        return self.flash_snapshots.chunk(snapshot_id, offset, length)

    def flash_snapshot_diff(self, before_id: str, after_id: str,
                            label: str = "") -> dict:
        """Diff two saved images offline and write complete JSON/CSV reports."""
        return self.flash_snapshots.diff(before_id, after_id, label)

    def flash_snapshot_diff_chunk(self, diff_id: str, format: str = "csv",
                                  offset: int = 0, length: int = 4096) -> dict:
        """Return a bounded saved diff-report fragment, without touching target."""
        return self.flash_snapshots.diff_chunk(diff_id, format, offset, length)

    def probe_target(self, speed_khz: int = 100, quirk: bool = False) -> dict:
        commands = ["init", "halt", "targets", "mdw 0xE0042000 1", "mdh 0x1FFFF7E0 1",
                    "flash probe 0", "flash info 0", "shutdown"]
        result = self._one_shot(speed_khz, commands, timeout=30, quirk=quirk)
        if result.get("ok"):
            result["flash_kib"] = parse_flash_kib(result["output"])
        return result

    def fault_snapshot(self, speed_khz: int = 100, quirk: bool = False) -> dict:
        commands = ["init", "halt", "reg", "mdw 0xE000ED28 1", "mdw 0xE000ED2C 1",
                    "mdw 0xE000ED34 1", "mdw 0xE000ED38 1", "shutdown"]
        return self._one_shot(speed_khz, commands, timeout=30, quirk=quirk)

    def start_gdb_server(self, speed_khz: int = 100, port: int = 3333, quirk: bool = False) -> dict:
        validate_speed(speed_khz)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise WorkerError("GDB port must be between 1024 and 65535")
        with self._lock:
            if self._server_running():
                return {"ok": True, "already_running": True, **self.server_status()}
            self._port = port
            log_path = self.workspace / "openocd-gdb.log"
            self._log_handle = log_path.open("ab")
            cmd = self._args(speed_khz, quirk)
            cmd += ["-c", "bindto 0.0.0.0", "-c", f"gdb_port {port}",
                    "-c", "telnet_port disabled", "-c", "tcl_port disabled"]
            try:
                self._process = subprocess.Popen(cmd, stdout=self._log_handle,
                                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                self._log_handle.close()
                self._log_handle = None
            except OSError as exc:
                self._log_handle.close()
                self._log_handle = None
                self._process = None
                return {"ok": False, "error": str(exc)}
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    return {"ok": False, "error": "OpenOCD exited before GDB became available", **self.server_status()}
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        return {"ok": True, **self.server_status()}
                except OSError:
                    time.sleep(0.2)
            failure = self.server_status()
            self.stop_gdb_server()  # Do not leave an unusable process holding the USB probe.
            return {"ok": False, "error": "GDB port not reachable after 12s",
                    "recent_log": failure["recent_log"], "stopped": True}

    def server_status(self) -> dict:
        with self._lock:
            running = self._server_running()
            log_path = self.workspace / "openocd-gdb.log"
            tail = ""
            if log_path.exists():
                with log_path.open("rb") as f:
                    f.seek(max(0, log_path.stat().st_size - MAX_LOG_CHARS))
                    tail = f.read().decode("utf-8", errors="replace")
            return {"running": running, "gdb_port": self._port, "gdb_host": socket.gethostname(),
                    "pid": self._process.pid if self._process else None, "recent_log": tail[-5000:]}

    def stop_gdb_server(self) -> dict:
        with self._lock:
            proc = self._process
            if proc is None:
                return {"ok": True, "stopped": False}
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            self._process = None
            return {"ok": True, "stopped": True, "exit_code": proc.returncode}

    def stage_hex(self, base64_hex: str, expected_sha256: str = "") -> dict:
        if len(base64_hex) > (MAX_HEX_BYTES * 4 // 3 + 16):
            raise WorkerError("Uploaded HEX too large")
        try:
            data = base64.b64decode(base64_hex, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise WorkerError("Invalid base64 HEX") from exc
        info = inspect_intel_hex(data)
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 and expected_sha256.lower() != digest:
            raise WorkerError("SHA-256 does not match uploaded data")
        artifact_id = uuid.uuid4().hex
        path = self.workspace / f"{artifact_id}.hex"
        path.write_bytes(data)
        with self._lock:
            self._images[artifact_id] = {"path": path, "digest": digest, **info}
        return {"ok": True, "artifact_id": artifact_id, "sha256": digest,
                "uploaded_bytes": len(data), **info}

    def staged_hex_info(self, artifact_id: str) -> dict:
        """Inspect an existing staged image without target access or mutation."""
        with self._lock:
            image = self._images.get(artifact_id)
            if not image:
                return {"ok": False, "error": "Unknown staged image"}
            path = image["path"]
            try:
                current = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            if current != image["digest"]:
                return {"ok": False, "error": "Staged file has changed"}
            name = str(path)
            if not re.fullmatch(r"/[A-Za-z0-9_./-]+", name):
                return {"ok": False, "error": "Staged path is not safe for the reviewed OpenOCD syntax"}
            return {"ok": True, "path": name, "sha256": current,
                    "start": image["start"], "end_exclusive": image["end_exclusive"],
                    "data_bytes": image["data_bytes"]}

    def flash_hex(self, artifact_id: str, confirm_sha256: str, confirm: str = "", speed_khz: int = 100,
                  quirk: bool = False) -> dict:
        if confirm != "FLASH":
            return {"ok": False, "error": "Explicit confirmation required: confirm='FLASH'"}
        with self._lock:
            if self._server_running():
                return {"ok": False, "error": "Stop GDB server before standalone flash. Alternatively use GDB 'load'."}
            image = self._images.get(artifact_id)
            if not image:
                return {"ok": False, "error": "Unknown staged image"}
            if image["digest"] != confirm_sha256.lower():
                return {"ok": False, "error": "SHA-256 confirmation does not match staged image"}
            if hashlib.sha256(image["path"].read_bytes()).hexdigest() != image["digest"]:
                return {"ok": False, "error": "Staged file has changed"}
            probe = self.probe_target(speed_khz, quirk)
            if not probe.get("ok"):
                return {"ok": False, "error": "Pre-flash probe failed", "probe": probe}
            cap = probe.get("flash_kib")
            if cap is None:
                return {"ok": False, "error": "Cannot read STM32 factory flash size; refusing flash", "probe": probe}
            if image["max_flash_kib_needed"] > cap:
                return {"ok": False, "error": f"Image needs {image['max_flash_kib_needed']} KiB, target reports {cap} KiB"}
            # UUID file names are Tcl-safe; no interpolated user path or arbitrary commands.
            cmd = ["program {" + image["path"].as_posix() + "} verify reset exit"]
            result = self._one_shot(speed_khz, cmd, timeout=180, quirk=quirk)
            result["target_flash_kib"] = cap
            result["sha256"] = image["digest"]
            return result
