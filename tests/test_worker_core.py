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

    def test_close_is_idempotent_and_releases_owned_gdb_once(self):
        # The explicit server finally block and the atexit fallback both
        # call close(); the worker must not terminate OpenOCD twice.
        with patch.object(self.worker, "stop_gdb_server",
                          return_value={"ok": True, "stopped": False}) as stop:
            self.worker.close()
            self.worker.close()
        stop.assert_called_once_with()
        self.assertTrue(self.worker._closed)

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

    def test_chunked_upload_integrates_with_legacy_staged_artifacts(self):
        image = good_hex()
        sha = hashlib.sha256(image).hexdigest()
        upload = self.worker.begin_firmware_upload(len(image), sha)
        upload_id = upload["upload_id"]
        self.assertEqual(upload["max_chunk_bytes"], 8192)
        first = image[:15]
        second = image[15:]
        self.worker.upload_firmware_chunk(upload_id, 0, base64.b64encode(first).decode())
        self.worker.upload_firmware_chunk(upload_id, len(first), base64.b64encode(second).decode())
        result = self.worker.finalize_firmware_upload(upload_id)
        self.assertEqual(result["sha256"], sha)
        self.assertEqual(result["uploaded_bytes"], len(image))
        self.assertTrue(self.worker.staged_hex_info(result["artifact_id"])["ok"])
        plan = self.worker.flash_preflight(result["artifact_id"])
        self.assertTrue(plan["ready_for_flash_request"])
        self.assertEqual(plan["target_flash_kib"], None)
        self.assertFalse(plan["target_access"])
        self.assertEqual(plan["capacity_status"], "NOT_PROBED_HOST_ONLY")

    def test_host_only_preflight_blocks_protected_nvm_without_probe(self):
        # One data byte in application, another in protected config at 0x08013800.
        image = (record(4, 0, b"\x08\x00") + record(0, 0, b"\x10") +
                 record(4, 0, b"\x08\x01") + record(0, 0x3800, b"\x20") +
                 record(1, 0)).encode()
        stage = self.worker.stage_hex(base64.b64encode(image).decode())
        with patch.object(self.worker, "_one_shot") as openocd:
            plan = self.worker.flash_preflight(stage["artifact_id"])
            self.assertTrue(plan["protected_overlap"])
            self.assertFalse(plan["ready_for_flash_request"])
            result = self.worker.flash_hex(stage["artifact_id"], stage["sha256"], confirm="FLASH")
            self.assertFalse(result["ok"])
            openocd.assert_not_called()

    def test_flash_preparation_explicit_reset_halt(self):
        stage = self.worker.stage_hex(base64.b64encode(good_hex()).decode())
        with patch.object(self.worker, "probe_target", return_value={"ok": True, "flash_kib": 128}):
            with patch.object(self.worker, "_one_shot", return_value={"ok": True}) as run:
                self.assertTrue(self.worker.flash_hex(stage["artifact_id"], stage["sha256"], confirm="FLASH")["ok"])
        command = run.call_args.args[1][0]
        self.assertIn("init; reset halt; program {", command)
        self.assertIn("verify reset exit", command)

    def test_missing_eof_has_actionable_code(self):
        from firmware_upload import UploadError
        with self.assertRaises(UploadError) as failure:
            inspect_intel_hex(good_hex().replace(b":00000001FF\n", b""))
        self.assertEqual(failure.exception.code, "HEX_MISSING_EOF")

    def test_new_status_tools_do_not_attach(self):
        with patch.object(self.worker._gdb_client, "_start") as start:
            self.assertTrue(self.worker.gdb_session_status()["ok"])
            self.assertTrue(self.worker.target_execution_status()["ok"])
            self.assertFalse(self.worker.target_execution_status(refresh=True)["ok"])
            self.assertFalse(self.worker.gdb_session_start()["ok"])
            self.assertFalse(self.worker.target_reset("run", "RESET")["ok"])
            start.assert_not_called()

    def test_reset_requires_confirmation_and_reports_observation(self):
        metadata = {"openocd_running": True, "session_state": "active",
                    "session_lost": False, "target_state": "unknown",
                    "target_state_verified": False}
        with patch.object(self.worker, "_gdb_metadata", return_value=metadata), \
                patch.object(self.worker._gdb_client, "command") as command:
            self.assertFalse(self.worker.target_reset("run", "")["ok"])
            command.assert_not_called()
            command.return_value = {"ok": True, "result_class": "done"}
            result = self.worker.target_reset("run", "RESET")
            self.assertTrue(result["reset_command_accepted"])
            self.assertFalse(result["requested_state_observed"])
            self.assertFalse(result["ok"])
            command.assert_called_once_with("monitor reset run", 3333, 30, "", require_existing=True)

    def test_reset_success_requires_matching_probe_and_session(self):
        before = {"openocd_running": True, "session_state": "active",
                  "session_lost": False, "target_state": "unknown",
                  "target_state_verified": False}
        after = {**before, "target_state": "running", "target_state_verified": True}
        with patch.object(self.worker, "_gdb_metadata", side_effect=[before, after]), \
                patch.object(self.worker._gdb_client, "command", return_value={
                    "ok": True, "result_class": "done", "state_probe": {"ok": True}}):
            result = self.worker.target_reset("run", "RESET", "expected")
            self.assertTrue(result["reset_command_accepted"], result)
            self.assertTrue(result["requested_state_observed"], result)
            self.assertTrue(result["ok"], result)
        with patch.object(self.worker, "_gdb_metadata", return_value=after), \
                patch.object(self.worker._gdb_client, "command", return_value={
                    "ok": False, "error": "GDB session_id mismatch"}):
            result = self.worker.target_reset("run", "RESET", "stale")
            self.assertFalse(result["reset_command_accepted"])
            self.assertFalse(result["requested_state_observed"])

    def test_fingerprint_live_capture_preserves_resume_metadata(self):
        from flash_snapshots import SnapshotError
        with patch.object(self.worker, "flash_snapshot", return_value={
                "ok": True, "snapshot_id": "a" * 32, "resumed": True, "flash_kib": 64}) as capture, \
                patch.object(self.worker.flash_snapshots, "fingerprint") as fingerprint:
            fingerprint.return_value = {"ok": True, "length": 65536}
            result = self.worker.firmware_fingerprint(capture_live=True)
            self.assertTrue(result["resumed"])
            self.assertEqual(result["source"], "live_snapshot")
            capture.assert_called_once_with(1000, "firmware-fingerprint", False)
            fingerprint.side_effect = SnapshotError("capacity exceeded")
            result = self.worker.firmware_fingerprint(capture_live=True, length=100000)
            self.assertFalse(result["ok"])
            self.assertTrue(result["resumed"])
            self.assertEqual(result["snapshot_id"], "a" * 32)

    def test_fingerprint_preserves_probe_exclusivity_and_offline_path(self):
        fake_proc = type("FakeProc", (), {"poll": lambda self: None, "pid": 1337})()
        self.worker._process = fake_proc
        try:
            with patch.object(self.worker.flash_snapshots, "capture") as capture, \
                    patch.object(self.worker.flash_snapshots, "fingerprint",
                                 return_value={"ok": True, "sha256": "test"}) as fingerprint:
                blocked = self.worker.firmware_fingerprint(capture_live=True)
                self.assertFalse(blocked["ok"])
                capture.assert_not_called()
                saved = self.worker.firmware_fingerprint(snapshot_id="a" * 32)
                self.assertTrue(saved["ok"])
                self.assertEqual(saved["source"], "saved_snapshot")
                fingerprint.assert_called_once_with("a" * 32, FLASH_START, 0, "")
        finally:
            self.worker._process = None

    def test_wrong_host_gdb_path_rejected_before_sending(self):
        from worker_core import pi_gdb_file_error
        result = pi_gdb_file_error("file /tmp/oberon-only-project.elf")
        self.assertEqual(result["error"]["code"], "GDB_FILE_NOT_ON_PI")
        self.assertIn("Raspberry Pi", result["error"]["message"])
        self.assertIsNone(pi_gdb_file_error("monitor reset halt"))
        with tempfile.NamedTemporaryFile(suffix=".elf") as tmp:
            self.assertIsNone(pi_gdb_file_error("file " + tmp.name))

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
