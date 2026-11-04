"""Persistent GDB/MI command transport for worker-managed OpenOCD.

Commands are forwarded verbatim to GDB's console interpreter, or as native
MI commands when prefixed with '-'.  A secret operator token is required for
each call because the GDB command language can modify the host and target.
"""
from __future__ import annotations

import ast
import hmac
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time

MAX_GDB_OUTPUT = 20000
MAX_QUEUED_LINES = 4096
_RESULT_RE = re.compile(r"^(\d+)\^(done|running|connected|error|exit)(?:,|$)")
_STREAM_RE = re.compile(r'^([~@&])("(?:\\.|[^"\\])*")$')
_ERROR_RE = re.compile(r'(?:^|,)msg=("(?:\\.|[^"\\])*")')


def _decode_mi_string(encoded: str) -> str:
    """Unescape GDB's C-style quoted MI string without executing any code."""
    try:
        return ast.literal_eval(encoded)
    except (ValueError, SyntaxError):
        return encoded


class LocalGDB:
    """One long-lived GDB process, serialized commands, bounded responses."""

    def __init__(self, executable: str | None = None):
        self.executable = executable or os.getenv("RPI5_GDB", "gdb-multiarch")
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue(maxsize=MAX_QUEUED_LINES)
        self._token = 0
        self._port: int | None = None
        self._dropped = 0

    def running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def close(self) -> None:
        """Stop only the local GDB process; no implicit target action."""
        with self._lock:
            proc = self._process
            self._process = None
            self._port = None
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
                if proc.stdin:
                    proc.stdin.close()
                if proc.stdout:
                    proc.stdout.close()
            self._lines = queue.Queue(maxsize=MAX_QUEUED_LINES)
            self._dropped = 0

    def _read_output(self, proc: subprocess.Popen, lines: queue.Queue[str | None]) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                item = line.rstrip("\r\n")
                try:
                    lines.put_nowait(item)
                except queue.Full:
                    try:
                        lines.get_nowait()
                    except queue.Empty:
                        pass
                    self._dropped += 1
                    try:
                        lines.put_nowait(item)
                    except queue.Full:
                        self._dropped += 1
        finally:
            try:
                lines.put_nowait(None)
            except queue.Full:
                pass

    def _send(self, mi_command: str, timeout: float) -> dict:
        proc = self._process
        assert proc is not None and proc.stdin is not None
        self._token += 1
        token = self._token
        try:
            proc.stdin.write(f"{token}{mi_command}\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            return {"ok": False, "error": f"GDB input failed: {exc}", "session_lost": True}

        deadline = time.monotonic() + timeout
        raw: list[str] = []
        visible: list[str] = []
        result_class = None
        result_record = ""
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                return {"ok": False, "error": "GDB process closed its output", "session_lost": True,
                        "output": "".join(visible)[-MAX_GDB_OUTPUT:]}
            raw.append(line)
            stream = _STREAM_RE.match(line)
            if stream:
                visible.append(_decode_mi_string(stream.group(2)))
            elif line.startswith(("*", "=", "+")):
                visible.append(line + "\n")
            result = _RESULT_RE.match(line)
            if result and int(result.group(1)) == token:
                result_class = result.group(2)
                result_record = line
                break

        raw_text = "\n".join(raw)
        output = "".join(visible)
        response = {
            "ok": result_class in ("done", "connected", "running"),
            "result_class": result_class,
            "output": output[-MAX_GDB_OUTPUT:],
            "raw_mi": raw_text[-MAX_GDB_OUTPUT:],
            "truncated": len(output) > MAX_GDB_OUTPUT or len(raw_text) > MAX_GDB_OUTPUT,
            "dropped_lines": self._dropped,
        }
        if result_class is None:
            response.update({"error": f"GDB did not complete within {timeout:g}s", "session_lost": True,
                             "target_state": "unknown"})
        elif result_class == "error":
            match = _ERROR_RE.search(result_record)
            response["error"] = _decode_mi_string(match.group(1)) if match else result_record
        elif result_class == "exit":
            response.update({"ok": False, "session_lost": True, "error": "GDB exited"})
        return response

    def _start(self, port: int) -> dict:
        executable = shutil.which(self.executable)
        if not executable:
            return {"ok": False, "error": f"GDB executable not found: {self.executable}; install gdb-multiarch on edge"}
        try:
            proc = subprocess.Popen(
                [executable, "--nx", "--nh", "--quiet", "--interpreter=mi2"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            return {"ok": False, "error": f"Cannot start GDB: {exc}"}
        self._process = proc
        self._port = port
        lines = self._lines
        threading.Thread(target=self._read_output, args=(proc, lines), daemon=True).start()
        for command in ("-gdb-set pagination off", "-gdb-set confirm off",
                        "-gdb-set mi-async on", f"-target-select extended-remote 127.0.0.1:{port}"):
            result = self._send(command, 15)
            if not result["ok"]:
                self.close()
                return {"ok": False, "error": f"Cannot initialize GDB with {command}: {result.get('error', result.get('raw_mi'))}"}
        return {"ok": True}

    def command(self, command: str, port: int, auth_token: str, timeout_seconds: int = 30) -> dict:
        """Forward one full GDB CLI or native MI command; empty string polls output."""
        secret = os.getenv("RPI5_GDB_COMMAND_TOKEN", "")
        if len(secret) < 32:
            return {"ok": False, "error": "GDB command forwarding disabled: set RPI5_GDB_COMMAND_TOKEN to a random 32+ character secret"}
        if type(auth_token) is not str or not hmac.compare_digest(auth_token, secret):
            return {"ok": False, "error": "Invalid GDB authorization token"}
        if type(command) is not str:
            raise ValueError("command must be a string")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be an integer from 1 to 300")
        with self._lock:
            if command == "":
                raw = []
                while True:
                    try:
                        line = self._lines.get_nowait()
                    except queue.Empty:
                        break
                    if line is not None:
                        raw.append(line)
                payload = "\n".join(raw)
                return {"ok": self.running(), "output": payload[-MAX_GDB_OUTPUT:],
                        "truncated": len(payload) > MAX_GDB_OUTPUT, "dropped_lines": self._dropped,
                        "gdb_running": self.running()}
            if self._process is None or self._process.poll() is not None or self._port != port:
                self.close()
                started = self._start(port)
                if not started["ok"]:
                    return started
            mi_command = command if command.startswith("-") else "-interpreter-exec console " + json.dumps(command, ensure_ascii=False)
            if command.startswith("-") and "\n" in command:
                return {"ok": False, "error": "Native MI protocol requests must occupy one line"}
            result = self._send(mi_command, timeout_seconds)
            result["command"] = command
            result["gdb_pid"] = self._process.pid if self._process else None
            if result.get("session_lost"):
                self.close()
            return result
