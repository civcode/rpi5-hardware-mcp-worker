import base64
import hashlib
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from worker_core import FLASH_START, OpenOCDWorker, WorkerError, inspect_intel_hex, parse_flash_kib, validate_speed


def record(kind, address, data=b''):
    payload = bytes([len(data), address >> 8, address & 255, kind]) + data
    return ':' + (payload + bytes([-sum(payload) & 255])).hex().upper() + '\n'


def good_hex():
    return (record(4, 0, b'\x08\x00') + record(0, 0, bytes(range(16))) + record(1, 0)).encode()


class FakeCompleted:
    def __init__(self, out=''):
        self.returncode = 0
        self.stdout = out


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.worker = OpenOCDWorker(openocd='fake-openocd', staging_dir=self.tmp.name)

    def tearDown(self):
        self.worker.close()
        self.tmp.cleanup()

    def test_speed_limits(self):
        self.assertEqual(validate_speed(100), 100)
        for n in (0, 9, 5001, '100', True):
            with self.assertRaises(WorkerError):
                validate_speed(n)

    def test_validate_hex(self):
        meta = inspect_intel_hex(good_hex())
        self.assertEqual(meta['start'], '0x08000000')
        self.assertEqual(meta['data_bytes'], 16)
        self.assertEqual(meta['max_flash_kib_needed'], 1)

    def test_reject_invalid_hex_checksum(self):
        with self.assertRaises(WorkerError):
            inspect_intel_hex(good_hex().replace(b':020000040800F2', b':020000040800F3'))

    def test_reject_hex_out_of_128k_bounds(self):
        far = (record(4, 0, b'\x08\x02') + record(0, 0, b'\x01') + record(1, 0)).encode()
        with self.assertRaisesRegex(WorkerError, 'outside'):
            inspect_intel_hex(far)

    def test_flash_capacity_parser(self):
        self.assertEqual(parse_flash_kib('0x1ffff7e0: 0080'), 128)
        self.assertEqual(parse_flash_kib('0x1ffff7e0: 0040'), 64)
        self.assertIsNone(parse_flash_kib('No target attached'))

    def test_host_config_inventory_is_bounded_fixed_and_never_inits_target(self):
        root = Path(self.tmp.name) / 'scripts'
        (root / 'target').mkdir(parents=True)
        (root / 'target/stm32f1x.cfg').write_text('reset_config srst_nogate\n')
        (root / 'target/unlisted.cfg').write_text('must not be returned')
        (root / 'mem_helper.tcl').write_bytes(b'x' * (128 * 1024 + 1))
        executable = Path(self.tmp.name) / 'openocd'
        executable.write_bytes(b'fake executable')
        version = type('Version', (), {'returncode': 0, 'stdout': '', 'stderr': 'fake version'})()
        with patch('worker_core.OPENOCD_SCRIPT_ROOTS', (str(root),)), \
             patch.dict('os.environ', {'OPENOCD_SCRIPTS': ''}), \
             patch('worker_core.shutil.which', return_value=str(executable)), \
             patch('worker_core.subprocess.run', return_value=version) as run:
            result = self.worker.inspect_openocd_host()
        self.assertEqual(result['executable_sha256'], hashlib.sha256(b'fake executable').hexdigest())
        self.assertEqual(run.call_args.args[0], [str(executable), '--version'])
        files = {s['path']: s for s in result['scripts']}
        self.assertEqual(files[str(root / 'target/stm32f1x.cfg')]['content'], 'reset_config srst_nogate\n')
        self.assertIn('size limit', files[str(root / 'mem_helper.tcl')]['error'])
        self.assertNotIn(str(root / 'target/unlisted.cfg'), files)
        self.assertIn('not proven', result['note'])

    def test_probe_uses_usb_cmsis_dap_and_never_flashes(self):
        output = '0x1ffff7e0: 0080\nflash size 128 KiB\n'
        with patch('subprocess.run', return_value=FakeCompleted(output)) as run:
            result = self.worker.probe_target(speed_khz=100)
        self.assertTrue(result['ok'])
        self.assertEqual(result['flash_kib'], 128)
        args = run.call_args.args[0]
        self.assertIn('interface/cmsis-dap.cfg', args)
        self.assertIn('target/stm32f1x.cfg', args)
        self.assertIn('adapter speed 100', args)
        self.assertNotIn('program', ' '.join(args))

    def test_staged_hex_info_rehashes_and_rejects_changed_and_unknown(self):
        stage = self.worker.stage_hex(base64.b64encode(good_hex()).decode())
        result = self.worker.staged_hex_info(stage['artifact_id'])
        self.assertTrue(result['ok'])
        self.assertEqual(result['sha256'], stage['sha256'])
        self.assertEqual(Path(result['path']).read_bytes(), good_hex())
        Path(result['path']).write_bytes(b'modified')
        self.assertFalse(self.worker.staged_hex_info(stage['artifact_id'])['ok'])
        self.assertFalse(self.worker.staged_hex_info('../escape')['ok'])

    def test_staged_hex_info_rejects_unsafe_tcl_path(self):
        with tempfile.TemporaryDirectory(prefix='unsafe brace } ') as directory:
            worker = OpenOCDWorker(openocd='fake', staging_dir=directory)
            item = worker.stage_hex(base64.b64encode(good_hex()).decode())
            self.assertFalse(worker.staged_hex_info(item['artifact_id'])['ok'])
            worker.close()

    def test_stage_flash_requires_double_confirmation(self):
        data = good_hex()
        sha = hashlib.sha256(data).hexdigest()
        item = self.worker.stage_hex(base64.b64encode(data).decode(), sha)
        self.assertEqual(item['sha256'], sha)
        self.assertFalse(self.worker.flash_hex(item['artifact_id'], sha)['ok'])
        self.assertFalse(self.worker.flash_hex(item['artifact_id'], '0'*64, confirm='FLASH')['ok'])

    def test_reject_flash_larger_than_physical_capacity(self):
        # Place one data byte at flash base and a byte at 64KiB + 1KiB.
        data = (record(4, 0, b'\x08\x00') + record(0, 0, b'\x00') +
                record(4, 0, b'\x08\x01') + record(0, 0x0400, b'\x00') + record(1, 0)).encode()
        stage = self.worker.stage_hex(base64.b64encode(data).decode())
        with patch.object(self.worker, 'probe_target', return_value={'ok': True, 'flash_kib': 64}):
            with patch.object(self.worker, '_one_shot') as run:
                outcome = self.worker.flash_hex(stage['artifact_id'], stage['sha256'], 'FLASH')
        self.assertFalse(outcome['ok'])
        self.assertIn('target reports 64 KiB', outcome['error'])
        run.assert_not_called()

    def test_flash_positive_requires_probed_capacity_and_verification(self):
        stage = self.worker.stage_hex(base64.b64encode(good_hex()).decode())
        with patch.object(self.worker, 'probe_target', return_value={'ok': True, 'flash_kib': 128}):
            with patch.object(self.worker, '_one_shot', return_value={'ok': True}) as run:
                result = self.worker.flash_hex(stage['artifact_id'], stage['sha256'], confirm='FLASH')
        self.assertTrue(result['ok'])
        cmd = run.call_args.args[1][0]
        self.assertIn('verify reset exit', cmd)
        self.assertIn('program', cmd)

    def test_gdb_server_lifecycle_with_fake_openocd(self):
        # Local stub binds the requested GDB port. No hardware is accessed.
        fake = Path(self.tmp.name) / "fake_openocd.py"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import socket, sys\n"
            "a = sys.argv\n"
            "port = 0\n"
            "for item in a:\n"
            "    if item.startswith('gdb_port '): port = int(item.split()[1])\n"
            "s = socket.socket()\n"
            "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            "s.bind(('127.0.0.1', port))\n"
            "s.listen(2)\n"
            "while True: conn, _ = s.accept(); conn.close()\n",
            encoding='utf-8',
        )
        fake.chmod(0o755)
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        self.worker.openocd = str(fake)
        result = self.worker.start_gdb_server(speed_khz=100, port=port)
        self.assertTrue(result['ok'], result)
        self.assertTrue(self.worker.server_status()['running'])
        stopped = self.worker.stop_gdb_server()
        self.assertTrue(stopped['stopped'])
        self.assertFalse(self.worker.server_status()['running'])

    def test_flash_snapshot_routes_to_bounded_store(self):
        with patch.object(self.worker.flash_snapshots, "capture",
                          return_value={"ok": True, "resumed": True}) as capture:
            result = self.worker.flash_snapshot(speed_khz=1000, label="stock-baseline")
        self.assertTrue(result["ok"])
        args, kwargs = capture.call_args
        self.assertIn("interface/cmsis-dap.cfg", args[0])
        self.assertIn("target/stm32f1x.cfg", args[0])
        self.assertIn("adapter speed 1000", args[0])
        self.assertEqual(args[2], "stock-baseline")

    def test_flash_snapshot_refuses_managed_gdb_probe_contention(self):
        fake_proc = type("FakeProc", (), {"poll": lambda self: None, "pid": 1337})()
        self.worker._process = fake_proc
        try:
            with patch.object(self.worker.flash_snapshots, "capture") as capture:
                result = self.worker.flash_snapshot()
            self.assertFalse(result["ok"])
            self.assertIn("GDB server owns", result["error"])
            capture.assert_not_called()
        finally:
            self.worker._process = None

    def test_refuse_one_shot_during_gdb(self):
        fake_proc = type('FakeProc', (), {'poll': lambda self: None, 'pid': 1337})()
        self.worker._process = fake_proc
        with patch('subprocess.run') as run:
            result = self.worker.probe_target()
        self.assertFalse(result['ok'])
        run.assert_not_called()
        self.worker._process = None


if __name__ == '__main__':
    unittest.main()
