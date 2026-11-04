"""Tests for the GDB/MI client using a fake executable, without hardware."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gdb_local import LocalGDB


STUB = '''#!{python}
import json, re, sys, time
counter = 0
for line in sys.stdin:
    found = re.match(r'(\\d+)(.*)', line.rstrip('\\n'))
    if not found:
        continue
    token, cmd = found.groups()
    if cmd.startswith('-interpreter-exec console '):
        arg = json.loads(cmd[len('-interpreter-exec console '):])
        if arg == 'timeout test':
            time.sleep(2)
        if arg == 'quit':
            print(token + '^exit', flush=True)
            break
        if arg == 'fail':
            print(token + '^error,msg="GDB command failed"', flush=True)
            continue
        if arg == 'continue':
            print(token + '^running', flush=True)
            print('*running,thread-id="all"', flush=True)
            continue
        counter += 1
        message = f'command={{arg!r}} count={{counter}}\\n'
        print('~' + json.dumps(message), flush=True)
    elif cmd.startswith('-exec-interrupt'):
        print('*stopped,reason="signal-received"', flush=True)
    print(token + '^done', flush=True)
'''


class TestLocalGDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.executable = Path(self.tmp.name) / 'fake_gdb'
        self.executable.write_text(STUB.format(python=os.sys.executable))
        self.executable.chmod(0o755)
        self.gdb = LocalGDB(executable=str(self.executable))
        self.patcher = patch.dict(os.environ, {"RPI5_GDB_COMMAND_TOKEN": "a" * 40})
        self.patcher.start()

    def tearDown(self):
        self.gdb.close()
        self.patcher.stop()
        self.tmp.cleanup()

    def test_console_commands_are_preserved_and_session_persists(self):
        first = self.gdb.command('set print pretty on', 3333, 'a' * 40)
        self.assertTrue(first['ok'], first)
        self.assertIn("command='set print pretty on' count=1", first['output'])
        second = self.gdb.command('set args --foo "bar"', 3333, 'a' * 40)
        self.assertTrue(second['ok'], second)
        self.assertIn('count=2', second['output'])
        self.assertEqual(first['gdb_pid'], second['gdb_pid'])

    def test_native_mi_command_and_async_running(self):
        start = self.gdb.command('continue', 3333, 'a' * 40)
        self.assertTrue(start['ok'], start)
        self.assertEqual(start['result_class'], 'running')
        stop = self.gdb.command('-exec-interrupt', 3333, 'a' * 40)
        self.assertTrue(stop['ok'], stop)
        self.assertIn('stopped', stop['output'])

    def test_error_does_not_reset_session(self):
        err = self.gdb.command('fail', 3333, 'a' * 40)
        self.assertFalse(err['ok'])
        self.assertEqual(err['error'], 'GDB command failed')
        self.assertTrue(self.gdb.command('info registers', 3333, 'a' * 40)['ok'])

    def test_quit_restarts_at_next_command(self):
        first = self.gdb.command('quit', 3333, 'a' * 40)
        self.assertFalse(first['ok'])
        self.assertFalse(self.gdb.running())
        self.assertTrue(self.gdb.command('info breakpoints', 3333, 'a' * 40)['ok'])

    def test_timeout_closes_client_and_reports_unknown_target_state(self):
        result = self.gdb.command('timeout test', 3333, 'a' * 40, timeout_seconds=1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['target_state'], 'unknown')
        self.assertFalse(self.gdb.running())

    def test_reject_bad_timeout_and_multiline_native_mi(self):
        with self.assertRaises(ValueError):
            self.gdb.command('info registers', 3333, 'a' * 40, timeout_seconds=0)
        result = self.gdb.command('-exec-continue\n-exec-interrupt', 3333, 'a' * 40)
        self.assertFalse(result['ok'])
        self.assertIn('one line', result['error'])

    def test_requires_authorization_before_process_start(self):
        denied = self.gdb.command("info registers", 3333, "wrong")
        self.assertFalse(denied["ok"])
        self.assertFalse(self.gdb.running())
        with patch.dict(os.environ, {"RPI5_GDB_COMMAND_TOKEN": ""}):
            disabled = self.gdb.command("info registers", 3333, "a" * 40)
        self.assertFalse(disabled["ok"])
        self.assertFalse(self.gdb.running())

    def test_missing_gdb_reports_error(self):
        with patch('gdb_local.shutil.which', return_value=None):
            result = self.gdb.command('info registers', 3333, 'a' * 40)
        self.assertFalse(result['ok'])
        self.assertIn('not found', result['error'])


if __name__ == '__main__':
    unittest.main()
