"""Local signal/shutdown tests; never touch SSH, SWD or the STM32."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import Mock

from worker_lifecycle import run_with_cleanup


class LifecycleTests(unittest.TestCase):
    def test_normal_server_return_calls_cleanup(self):
        run = Mock()
        close = Mock()
        run_with_cleanup(run, close)
        run.assert_called_once_with()
        close.assert_called_once_with()

    def test_keyboard_interrupt_is_handled(self):
        run = Mock(side_effect=KeyboardInterrupt)
        close = Mock()
        run_with_cleanup(run, close)
        close.assert_called_once_with()

    def test_other_error_propagates_after_cleanup(self):
        run = Mock(side_effect=RuntimeError("failed to start"))
        close = Mock()
        with self.assertRaisesRegex(RuntimeError, "failed to start"):
            run_with_cleanup(run, close)
        close.assert_called_once_with()

    @unittest.skipUnless(os.name == "posix", "SIGINT process test requires POSIX")
    def test_real_sigint_gracefully_exits_process(self):
        # Standalone Python harness exercises real Ctrl+C/SIGINT without
        # bringing up Uvicorn, OpenOCD, the USB probe or the target.
        script = textwrap.dedent("""
            import time
            from worker_lifecycle import run_with_cleanup

            def run():
                print("READY", flush=True)
                while True:
                    time.sleep(0.05)

            def close():
                print("CLEANED", flush=True)

            run_with_cleanup(run, close)
        """)
        proc = subprocess.Popen([sys.executable, "-c", script],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            # Wait for a startup handshake rather than sending SIGINT while
            # Python is still importing modules.
            import select
            ready, _, _ = select.select([proc.stdout], [], [], 5)
            self.assertTrue(ready, "child process did not become ready")
            self.assertEqual(proc.stdout.readline().strip(), "READY")
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertIn("CLEANED", stdout)
            self.assertIn("Hardware MCP worker stopped.", stderr)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
