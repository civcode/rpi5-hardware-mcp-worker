"""Explicit lifecycle and execution-state observation for the local GDB transport."""
from __future__ import annotations

import queue
import re
import threading
import time
import uuid

from gdb_local import LocalGDB

_TARGET_ROW = re.compile(
    r"^\s*\d+\*?\s+\S+\s+\S+\s+\S+\s+\S+\s+"
    r"(halted|running|reset|unknown|debug-running|unavailable)\s*$", re.MULTILINE
)
_MONITOR_STATE_CHANGES = {"monitor reset run", "monitor reset halt",
                          "monitor halt", "monitor resume"}


def monitor_target_state(output: str) -> str:
    """Accept only an unambiguous single OpenOCD target-table row."""
    rows = _TARGET_ROW.findall(output)
    return rows[0] if len(rows) == 1 else "unknown"


class ManagedGDB(LocalGDB):
    """Keep a session identity across commands; never silently replace a lost session.

    All commands use the inherited command lock. The reader has its own lock so
    async events can arrive even while the command thread is waiting for MI.
    """

    def __init__(self, executable: str | None = None):
        super().__init__(executable)
        self._event_lock = threading.Lock()
        self._session_id = None
        self._previous_pid = None
        self._allow_implicit = True
        self._phase = "not_started"
        self._loss_reason = None
        self._exit_code = None
        self._eof = False
        self._event_seq = 0
        self._state = "unknown"
        self._source = None
        self._observed_at = None

    def _observe(self, state: str, source: str | None) -> None:
        # Caller holds _event_lock.
        self._state = state
        self._source = source
        self._observed_at = time.time() if source else None

    def _read_output(self, proc, lines) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                item = line.rstrip("\r\n")
                with self._event_lock:
                    if proc is self._process:
                        if item.startswith(("*running", "*stopped")):
                            self._event_seq += 1
                            state = "running" if item.startswith("*running") else "halted"
                            self._observe(state, "gdb_mi")
                        elif item.startswith("=thread-group-exited"):
                            self._event_seq += 1
                            self._observe("unknown", None)
                            self._loss_reason = "GDB inferior exited or disconnected"
                try:
                    lines.put_nowait(item)
                except queue.Full:
                    try:
                        lines.get_nowait()
                    except queue.Empty:
                        pass
                    if proc is self._process:
                        self._dropped += 1
                    try:
                        lines.put_nowait(item)
                    except queue.Full:
                        if proc is self._process:
                            self._dropped += 1
        finally:
            with self._event_lock:
                if proc is self._process:
                    self._eof = True
                    self._observe("unknown", None)
            # Ensure EOF is delivered even if an async event flood filled the queue.
            try:
                lines.put_nowait(None)
            except queue.Full:
                try:
                    lines.get_nowait()
                except queue.Empty:
                    pass
                lines.put_nowait(None)

    def status(self) -> dict:
        """Host-only observation: no attach, MI request, reset, halt or resume."""
        with self._lock:
            proc = self._process
            exit_code = proc.poll() if proc is not None else None
            with self._event_lock:
                alive = proc is not None and exit_code is None
                if self._phase in ("active", "starting") and (
                        not alive or self._eof or self._loss_reason):
                    self._phase = "lost"
                    self._loss_reason = self._loss_reason or (
                        "GDB output closed" if self._eof else "GDB process exited")
                    self._exit_code = exit_code
                    self._observe("unknown", None)
                active = self._phase == "active" and alive
                return {
                    "session_id": self._session_id,
                    "session_state": self._phase,
                    "session_lost": self._phase == "lost",
                    "session_loss_reason": self._loss_reason,
                    "gdb_running": alive,
                    "gdb_pid": proc.pid if alive else None,
                    "last_gdb_pid": self._previous_pid,
                    "gdb_exit_code": self._exit_code,
                    "target_state": self._state if active else "unknown",
                    "target_state_source": self._source if active else None,
                    "target_state_observed_at": self._observed_at if active else None,
                    # MI reports GDB's view, not necessarily hardware state after
                    # an OpenOCD monitor command. Only a direct targets response
                    # qualifies as an OpenOCD observation (not a live guarantee).
                    "target_state_verified": active and self._source == "openocd_targets"
                    and self._state != "unknown",
                    "session_recreated": False,
                }

    def close(self, reason: str = "explicit_stop", *, lost: bool = False) -> None:
        """Terminate owned GDB; never issue an implicit halt/resume/reset command."""
        with self._lock:
            proc = self._process
            if proc is not None:
                self._previous_pid = proc.pid
                code = proc.poll()
                if code is not None:
                    self._exit_code = code
            super().close()
            self._allow_implicit = False
            with self._event_lock:
                self._phase = "lost" if lost else "stopped"
                self._loss_reason = reason if lost else None
                self._eof = False
                self._observe("unknown", None)

    def prepare_server(self) -> None:
        """An explicitly started new OpenOCD epoch permits one legacy lazy attach."""
        with self._lock:
            self.close()
            self._allow_implicit = True
            self._phase = "not_started"

    def start(self, port: int) -> dict:
        with self._lock:
            before = self.status()
            if before["session_state"] == "active" and self._port == port:
                return {"ok": True, "already_running": True, **before}
            recreated = self._session_id is not None
            self.close()
            self._session_id = uuid.uuid4().hex
            self._exit_code = None
            self._phase = "starting"
            # Parent _start may close the process on initialization failure.
            result = self._start(port)
            if not result["ok"]:
                self.close(result.get("error", "GDB initialization failed"), lost=True)
                return {**result, **self.status()}
            self._phase = "active"
            current = self.status()
            if current["session_lost"]:
                self.close(current["session_loss_reason"], lost=True)
                return {"ok": False, "error": "GDB lost during attach", **self.status()}
            return {"ok": True, **current, "session_recreated": recreated}

    def _probe_state(self, timeout: float) -> dict:
        with self._event_lock:
            seq = self._event_seq
            self._observe("unknown", None)
        result = self._send('-interpreter-exec console "monitor targets"', timeout)
        if result.get("session_lost"):
            self.close(result.get("error", "GDB lost during state probe"), lost=True)
        elif result["ok"]:
            observed = monitor_target_state(result.get("output", ""))
            with self._event_lock:
                # Do not overwrite a newer async event with a potentially older
                # hardware observation; conservative unknown on disagreement.
                if seq == self._event_seq or self._state == observed:
                    self._observe(observed, "openocd_targets" if observed != "unknown" else None)
                else:
                    self._observe("unknown", None)
        return result

    def execution_status(self, refresh: bool = False, timeout_seconds: int = 5) -> dict:
        with self._lock:
            status = self.status()
            if not refresh:
                return {"ok": True, **status}
            if status["session_state"] != "active":
                return {"ok": False, "error": "Explicit gdb_session_start required", **status}
            result = self._probe_state(timeout_seconds)
            status = self.status()
            return {"ok": result["ok"] and status["target_state_verified"] and not status["session_lost"],
                    **status, "state_probe": result}

    def command(self, command: str, port: int, timeout_seconds: int = 30,
                session_id: str = "", *, require_existing: bool = False) -> dict:
        if type(command) is not str:
            raise ValueError("command must be a string")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be an integer from 1 to 300")
        if command.startswith("-") and ("\n" in command or "\r" in command):
            return {"ok": False, "error": "Native MI protocol requests must occupy one line",
                    **self.status()}
        with self._lock:
            status = self.status()
            if session_id and session_id != status["session_id"]:
                return {"ok": False, "error": "GDB session_id mismatch; command not sent", **status}
            if status["session_state"] != "active" or self._port != port:
                if (not command or require_existing or session_id or not self._allow_implicit
                        or status["session_lost"]):
                    return {"ok": False, "error": "Explicit gdb_session_start required; command not sent",
                            **status}
                started = self.start(port)
                if not started["ok"]:
                    return started
                recreated = started["session_recreated"]
            else:
                recreated = False
            # Parent.command can auto-start, so never call it for nonempty
            # commands: a process dying between check and send must fail closed.
            if not command:
                result = super().command("", port, timeout_seconds)
                current = self.status()
                return {**result, **current, "ok": result["ok"] and not current["session_lost"]}
            import json
            mi = command if command.startswith("-") else (
                "-interpreter-exec console " + json.dumps(command, ensure_ascii=False))
            normalized = " ".join(command.strip().split()).lower()
            with self._event_lock:
                # The arbitrary command language can change state. Do not
                # promote stale observations to evidence for a new command.
                self._observe("unknown", None)
            result = self._send(mi, timeout_seconds)
            result["command"] = command
            if result.get("session_lost"):
                self.close(result.get("error", "GDB session lost"), lost=True)
            elif result["ok"] and (normalized in {"detach", "disconnect", "-target-detach",
                                                   "-target-disconnect"}):
                self.close("GDB target detached/disconnected", lost=True)
            elif result["ok"] and normalized in _MONITOR_STATE_CHANGES:
                # Best effort only: GDB may reject a monitor query while running.
                result["state_probe"] = self._probe_state(min(5, timeout_seconds))
            current = self.status()
            return {**result, **current, "session_recreated": recreated,
                    "ok": result["ok"] and not current["session_lost"]}
