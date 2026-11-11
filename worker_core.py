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
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid

from firmware_upload import FirmwareUploadManager, UploadError
from flash_snapshots import FlashSnapshotStore
from gdb_local import LocalGDB


FLASH_START = 0x08000000
FLASH_LIMIT = FLASH_START + 128 * 1024
# ATfE application must not overwrite original NVM/configuration at 0x08013800.
APP_FLASH_LIMIT = 0x08013800
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
    if not saw_eof:
        raise UploadError("HEX_MISSING_EOF", "HEX file is missing the final EOF record :00000001FF",
                          decoded_bytes=len(contents))
    if total <= 0 or min_addr is None or max_exclusive is None:
        raise UploadError("HEX_NO_DATA", "HEX file contains no flash data records",
                          decoded_bytes=len(contents))
    if min_addr != FLASH_START:
        raise WorkerError("HEX image must begin at STM32 flash base 0x08000000")
    return {
        "start": f"0x{min_addr:08x}", "end_exclusive": f"0x{max_exclusive:08x}",
        "max_flash_kib_needed": (max_exclusive - FLASH_START + 1023) // 1024,
        "data_bytes": total,
    }


def pi_gdb_file_error(command: str) -> dict | None:
    """Explain wrong-host file paths before GDB/OpenOCD attempts filesystem I/O.

    Covers ordinary GDB console commands. More complex MI commands are
    intentionally not rewritten or executed as filesystem operations here.
    """
    try:
        fields = shlex.split(command)
    except ValueError:
        return None
    if not fields:
        return None
    filename = None
    if fields[0] in ("file", "exec-file", "symbol-file", "add-symbol-file", "load") and len(fields) >= 2:
        filename = fields[1]
    elif fields[:3] == ["monitor", "flash", "write_image"] and len(fields) >= 5:
        filename = fields[4] if fields[3] in ("erase", "unlock", "erase_unlock") else fields[3]
    elif fields[:2] == ["monitor", "verify_image"] and len(fields) >= 3:
        filename = fields[2]
    if filename and not Path(filename).is_file():
        return {"ok": False, "error": {"code": "GDB_FILE_NOT_ON_PI",
                "message": "This MCP GDB/OpenOCD process runs on the Raspberry Pi; file does not exist there",
                "details": {"path": filename, "hint": "Stage the file on the Pi, or run GDB on Oberon against the Pi OpenOCD server."}}}
    return None


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
        self._upload_manager = FirmwareUploadManager(self.workspace, inspect_intel_hex, MAX_HEX_BYTES)
        self.flash_snapshots = FlashSnapshotStore()
        self._gdb_client = LocalGDB()
        self._closed = False

    def close(self) -> None:
        """Stop owned GDB/OpenOCD, then release transient workspace once.

        Safe to invoke both in the explicit server shutdown and at interpreter
        exit. Persistent flash snapshots are deliberately retained.
        """
        with self._lock:
            if self._closed:
                return
            self.stop_gdb_server()
            self._upload_manager.close()
            if self._owns_workspace:
                shutil.rmtree(self.workspace, ignore_errors=True)
            self._closed = True

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

    def gdb_command(self, command: str, timeout_seconds: int = 30) -> dict:
        """Send a command to the owned, persistent local GDB process."""
        with self._lock:
            if not self._server_running():
                self._gdb_client.close()
                return {"ok": False, "error": "OpenOCD server unavailable; start_gdb_server is required"}
            wrong_host = pi_gdb_file_error(command)
            if wrong_host:
                return wrong_host
            return self._gdb_client.command(command, self._port, timeout_seconds)

    def start_gdb_server(self, speed_khz: int = 100, port: int = 3333, quirk: bool = False) -> dict:
        validate_speed(speed_khz)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise WorkerError("GDB port must be between 1024 and 65535")
        with self._lock:
            if self._server_running():
                return {"ok": True, "already_running": True, **self.server_status()}
            self._gdb_client.close()
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
            return {"running": running, "local_gdb_running": self._gdb_client.running(),
                    "gdb_port": self._port, "gdb_host": socket.gethostname(),
                    "pid": self._process.pid if self._process else None, "recent_log": tail[-5000:]}

    def stop_gdb_server(self) -> dict:
        with self._lock:
            self._gdb_client.close()
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

    def begin_firmware_upload(self, total_bytes: int, expected_sha256: str) -> dict:
        """Create a bounded, host-only transfer session; does not touch target."""
        return self._upload_manager.begin(total_bytes, expected_sha256)

    def upload_firmware_chunk(self, upload_id: str, offset: int, chunk_base64: str) -> dict:
        """Write at most 8192 decoded bytes to the Pi workspace, in byte order."""
        return self._upload_manager.chunk(upload_id, offset, chunk_base64)

    def finalize_firmware_upload(self, upload_id: str) -> dict:
        """Require exact length, SHA-256 and valid flash-range Intel HEX before staging."""
        result = self._upload_manager.finalize(upload_id)
        with self._lock:
            self._images[result["artifact_id"]] = {
                "path": result["path"], "digest": result["sha256"],
                "start": result["start"], "end_exclusive": result["end_exclusive"],
                "data_bytes": result["data_bytes"],
                "max_flash_kib_needed": result["max_flash_kib_needed"],
            }
        return {key: value for key, value in result.items() if key != "path"}

    def abort_firmware_upload(self, upload_id: str) -> dict:
        """Cancel a partial upload and remove its temporary file; never touches target."""
        return self._upload_manager.abort(upload_id)

    def flash_preflight(self, artifact_id: str) -> dict:
        """Non-disruptive host-only flash plan; hardware capacity checked during flash_hex.

        Does not reset, halt, resume, attach OpenOCD or transmit UART data.
        """
        with self._lock:
            image = self._images.get(artifact_id)
            if image is None:
                return {"ok": False, "error": {"code": "ARTIFACT_UNKNOWN",
                        "message": "Unknown staged image; stage it again after worker restart"}}
            try:
                contents = image["path"].read_bytes()
            except OSError as exc:
                return {"ok": False, "error": {"code": "ARTIFACT_READ_FAILED", "message": str(exc)}}
            digest = hashlib.sha256(contents).hexdigest()
            if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(image["path"])):
                return {"ok": False, "error": {"code": "STAGED_PATH_UNSAFE",
                        "message": "Pi workspace path is unsafe for the OpenOCD command syntax"}}
            if digest != image["digest"]:
                return {"ok": False, "error": {"code": "ARTIFACT_CHANGED",
                        "message": "Staged image SHA-256 changed; refuse flash"}}
            info = inspect_intel_hex(contents)
            end = int(info["end_exclusive"], 16)
            protected_overlap = end > APP_FLASH_LIMIT
            server_busy = self._server_running()
            reasons = []
            if protected_overlap:
                reasons.append("IMAGE_OVERLAPS_PROTECTED_FLASH")
            if server_busy:
                reasons.append("GDB_SERVER_OWNS_PROBE")
            return {
                "ok": True, "artifact_id": artifact_id, "sha256": digest,
                "uploaded_bytes": len(contents), "data_bytes": info["data_bytes"],
                "start": info["start"], "end_exclusive": info["end_exclusive"],
                "required_flash_kib": info["max_flash_kib_needed"],
                "protected_region": {"start": f"0x{APP_FLASH_LIMIT:08x}",
                                     "end_exclusive": f"0x{FLASH_LIMIT:08x}"},
                "protected_overlap": protected_overlap,
                "gdb_server_owns_probe": server_busy,
                "target_flash_kib": None,
                "capacity_status": "NOT_PROBED_HOST_ONLY",
                "capacity_note": "Actual factory flash size is verified by flash_hex before programming.",
                "will_reset_halt_before_programming": True,
                "ready_for_flash_request": not reasons,
                "blocking_reasons": reasons,
                "target_access": False,
            }

    def stage_hex(self, base64_hex: str, expected_sha256: str = "") -> dict:
        if len(base64_hex) > (MAX_HEX_BYTES * 4 // 3 + 16):
            raise WorkerError("Uploaded HEX too large")
        try:
            data = base64.b64decode(base64_hex, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise WorkerError("Invalid base64 HEX") from exc
        if not data:
            raise UploadError("UPLOAD_EMPTY", "Base64 argument decoded to zero bytes; pass complete file contents, not a filename")
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
            preflight = self.flash_preflight(artifact_id)
            if not preflight.get("ok"):
                return preflight
            if not preflight["ready_for_flash_request"]:
                return {"ok": False, "error": "Flash preflight blocked programming",
                        "blocking_reasons": preflight["blocking_reasons"], "preflight": preflight}
            image = self._images.get(artifact_id)
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
            # reset halt is required: the earlier H14 halt-only attempt faulted in
            # OpenOCD's flash loader; the controlled reset-halt retry succeeded.
            cmd = ["init; reset halt; program {" + image["path"].as_posix() + "} verify reset exit"]
            result = self._one_shot(speed_khz, cmd, timeout=180, quirk=quirk)
            result["target_flash_kib"] = cap
            result["sha256"] = image["digest"]
            return result
