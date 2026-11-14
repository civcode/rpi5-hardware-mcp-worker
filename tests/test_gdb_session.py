"""Regression tests for managed sessions using a fake MI subprocess; no hardware."""
import os
from pathlib import Path
import tempfile
import time
import unittest

from gdb_session import ManagedGDB, monitor_target_state


STUB = '''#!{python}
import json, os, re, sys, time
state = "halted"
for line in sys.stdin:
    token, cmd = re.match(r'(\\d+)(.*)', line.rstrip('\\n')).groups()
    if cmd.startswith('-target-select'):
        print('*stopped,reason="signal-received"', flush=True)
        print(token + '^connected', flush=True)
        continue
    if cmd.startswith('-interpreter-exec console '):
        arg = json.loads(cmd[len('-interpreter-exec console '):])
        if arg == "die":
            os._exit(17)
        if arg == "timeout":
            time.sleep(3)
        if arg == "quit":
            print(token + "^exit", flush=True)
            break
        if arg == "monitor reset run" or arg == "monitor resume":
            state = "running"
        if arg == "monitor reset halt" or arg == "monitor halt":
            state = "halted"
        if arg == "monitor targets":
            if state == "unavailable":
                print(token + '^error,msg="target unavailable"', flush=True)
                continue
            row = " 0* stm32f1x.cpu cortex_m little stm32f1x.cpu " + state + "\\\\n"
            print("@" + json.dumps(row.replace("\\\\n", "\\n")), flush=True)
        if arg == "unavailable":
            state = "unavailable"
        if arg == "async":
            print(token + "^running", flush=True)
            time.sleep(0.05)
            print('*running,thread-id="all"', flush=True)
            time.sleep(0.05)
            print('*stopped,reason="breakpoint-hit"', flush=True)
            continue
        if arg == "disconnect":
            print('=thread-group-exited,id="i1"', flush=True)
    print(token + "^done", flush=True)
'''


class ManagedSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        executable = Path(self.tmp.name) / "fake-gdb"
        executable.write_text(STUB.format(python=os.sys.executable))
        executable.chmod(0o755)
        self.gdb = ManagedGDB(str(executable))
        self.addCleanup(self.gdb.close)

    def test_lazy_first_attach_then_stable_session_and_pid(self):
        self.assertEqual(self.gdb.status()["session_state"], "not_started")
        self.assertFalse(self.gdb.command("", 3333)["ok"])
        first = self.gdb.command("info registers", 3333)
        second = self.gdb.command("info registers", 3333, session_id=first["session_id"])
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(first["gdb_pid"], second["gdb_pid"])
        self.assertFalse(second["session_recreated"])
        self.assertTrue(self.gdb.start(3333)["already_running"])

    def test_process_death_requires_explicit_new_session(self):
        first = self.gdb.start(3333)
        failed = self.gdb.command("die", 3333)
        self.assertFalse(failed["ok"], failed)
        self.assertTrue(failed["session_lost"], failed)
        blocked = self.gdb.command("info registers", 3333)
        self.assertFalse(blocked["ok"], blocked)
        self.assertEqual(blocked["session_id"], first["session_id"])
        self.assertEqual(blocked["last_gdb_pid"], first["gdb_pid"])
        new = self.gdb.start(3333)
        self.assertTrue(new["ok"], new)
        self.assertTrue(new["session_recreated"])
        self.assertNotEqual(new["session_id"], first["session_id"])
        stale = self.gdb.command("monitor reset run", 3333, session_id=first["session_id"])
        self.assertFalse(stale["ok"])
        self.assertIn("mismatch", stale["error"])

    def test_timeout_and_quit_never_retry(self):
        self.gdb.start(3333)
        timed = self.gdb.command("timeout", 3333, timeout_seconds=1)
        self.assertTrue(timed["session_lost"], timed)
        self.assertEqual(timed["target_state"], "unknown")
        self.assertFalse(self.gdb.command("info registers", 3333)["ok"])
        self.gdb.start(3333)
        quit_result = self.gdb.command("quit", 3333)
        self.assertTrue(quit_result["session_lost"])
        self.assertFalse(self.gdb.command("info registers", 3333)["ok"])

    def test_explicit_stop_does_not_auto_attach(self):
        self.gdb.start(3333)
        self.gdb.close()
        self.assertEqual(self.gdb.status()["session_state"], "stopped")
        self.assertFalse(self.gdb.command("info registers", 3333)["ok"])
        self.assertTrue(self.gdb.start(3333)["session_recreated"])

    def test_run_and_halt_probe_observed_state(self):
        self.gdb.start(3333)
        for mode, wanted in (("run", "running"), ("halt", "halted")):
            result = self.gdb.command("monitor reset " + mode, 3333)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["result_class"], "done")
            self.assertEqual(result["target_state"], wanted)
            self.assertTrue(result["target_state_verified"], result)
            self.assertEqual(result["target_state_source"], "openocd_targets")

    def test_failed_probe_does_not_invent_state(self):
        self.gdb.start(3333)
        self.gdb.command("unavailable", 3333)
        result = self.gdb.execution_status(refresh=True)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["target_state"], "unknown")
        self.assertFalse(result["target_state_verified"])

    def test_async_records_after_result_seen_without_poll_or_command(self):
        self.gdb.start(3333)
        result = self.gdb.command("async", 3333)
        self.assertEqual(result["result_class"], "running")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = self.gdb.status()
            if status["target_state"] == "halted":
                break
            time.sleep(0.01)
        self.assertEqual(status["target_state"], "halted", status)
        self.assertEqual(status["target_state_source"], "gdb_mi")
        self.assertFalse(status["target_state_verified"])

    def test_disconnect_blocks_commands(self):
        self.gdb.start(3333)
        result = self.gdb.command("disconnect", 3333)
        self.assertTrue(result["session_lost"], result)
        self.assertFalse(self.gdb.command("info registers", 3333)["ok"])

    def test_parser_requires_single_valid_row(self):
        row = " 0* stm32f1x.cpu cortex_m little stm32f1x.cpu halted\n"
        self.assertEqual(monitor_target_state(row), "halted")
        self.assertEqual(monitor_target_state(row + row), "unknown")
        self.assertEqual(monitor_target_state("reset run succeeded"), "unknown")


if __name__ == "__main__":
    unittest.main()
