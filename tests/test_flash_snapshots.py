"""Offline tests: no USB, SWD, UART, device flash, or GitHub Actions."""
import base64
import csv
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from flash_snapshots import FlashSnapshotStore, SnapshotError, analyze_difference, snapshot_tool_result


class Completed:
    returncode = 0
    stdout = "FLASH_SNAPSHOT_KIB=128\nFLASH_SNAPSHOT_DUMP_OK\nFLASH_SNAPSHOT_RESUME_OK\n"


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = FlashSnapshotStore(self.root / "saved")
        self.work = self.root / "worker"
        self.work.mkdir()
        self.first = bytes(128 * 1024)
        self.second = bytearray(self.first)
        self.second[78 * 1024 + 0x43] = 1
        self.second[78 * 1024 + 0x44] = 2
        self.second[78 * 1024 + 0x90] = 7

    def capture(self, data, kib=128, output=None, exit_code=0):
        def fake_run(argv, **kwargs):
            self.assertTrue(any("init" == x for x in argv))
            script = argv[-1]
            self.assertIn("read_memory 0x1FFFF7E0 16 1", script)
            self.assertIn("dump_image", script)
            self.assertIn("catch {resume}", script)
            self.assertNotIn("reset", script)
            self.assertNotIn("flash write", script)
            self.assertNotIn("program ", script)
            match = re.search(r"dump_image \{([^}]+)\}", script)
            self.assertIsNotNone(match)
            Path(match.group(1)).write_bytes(data)
            r = Completed()
            r.returncode = exit_code
            r.stdout = output if output is not None else f"FLASH_SNAPSHOT_KIB={kib}\nFLASH_SNAPSHOT_DUMP_OK\nFLASH_SNAPSHOT_RESUME_OK\n"
            return r
        with patch("flash_snapshots.subprocess.run", side_effect=fake_run):
            return self.store.capture(["openocd", "-f", "target/stm32f1x.cfg"], self.work, "test")

    def test_fingerprint_ignores_configuration_outside_application(self):
        before = self.capture(self.first)["snapshot_id"]
        after = self.capture(bytes(self.second))["snapshot_id"]
        left = self.store.fingerprint(before)
        right = self.store.fingerprint(after, expected_sha256=left["sha256"])
        self.assertTrue(right["matches_expected"], right)
        self.assertEqual(left["length"], 78 * 1024)
        self.assertNotEqual(left["snapshot_sha256"], right["snapshot_sha256"])
        config = self.store.fingerprint(after, 0x08013800, 1024)
        self.assertTrue(config["includes_protected_region"])

    def test_fingerprint_capacity_integrity_and_exact_range(self):
        snap = self.capture(self.first[:65536], kib=64)["snapshot_id"]
        result = self.store.fingerprint(snap)
        self.assertEqual(result["length"], 65536)
        self.assertEqual(result["sha256"], hashlib.sha256(self.first[:65536]).hexdigest())
        for start, length in ((0x0800ffff, 2), (0x07ffffff, 1),
                              (0x08000000, -1), (True, 1)):
            with self.assertRaises(SnapshotError):
                self.store.fingerprint(snap, start, length)
        with self.assertRaises(SnapshotError):
            self.store.fingerprint(snap, expected_sha256="not-a-hash")
        piece = self.store.fingerprint(snap, 0x08000003, 7)
        self.assertEqual(piece["sha256"], hashlib.sha256(self.first[3:10]).hexdigest())
        Path(self.store.info(snap)["path"]).write_bytes(b"corrupt")
        with self.assertRaises(SnapshotError):
            self.store.fingerprint(snap)

    def test_capture_persists_sha_and_resumes(self):
        result = self.capture(self.first)
        self.assertTrue(result["ok"])
        self.assertEqual(result["sha256"], hashlib.sha256(self.first).hexdigest())
        self.assertTrue(result["resumed"])
        self.assertEqual(self.store.info(result["snapshot_id"])["length"], 131072)
        self.assertEqual(FlashSnapshotStore(self.store.directory).list_snapshots()["total"], 1)
        self.assertFalse(any(self.work.iterdir()))

    def test_64k_board_is_supported(self):
        result = self.capture(self.first[:65536], kib=64)
        self.assertTrue(result["ok"])
        self.assertEqual(result["flash_kib"], 64)

    def test_openocd_hex_capacity_128_kib_is_accepted(self):
        result = self.capture(
            self.first, output="FLASH_SNAPSHOT_KIB=0x80\nFLASH_SNAPSHOT_DUMP_OK\nFLASH_SNAPSHOT_RESUME_OK\n")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["flash_kib"], 128)
        self.assertEqual(result["length"], 131072)
        self.assertEqual(self.store.info(result["snapshot_id"])["sha256"],
                         hashlib.sha256(self.first).hexdigest())

    def test_openocd_hex_capacity_64_kib_is_accepted(self):
        result = self.capture(
            self.first[:65536], kib=64,
            output="FLASH_SNAPSHOT_KIB=0x40\nFLASH_SNAPSHOT_DUMP_OK\nFLASH_SNAPSHOT_RESUME_OK\n")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["flash_kib"], 64)

    def test_hex_capacity_still_rejects_unsupported_size(self):
        result = self.capture(
            self.first, output="FLASH_SNAPSHOT_KIB=0x100\nFLASH_SNAPSHOT_DUMP_OK\nFLASH_SNAPSHOT_RESUME_OK\n")
        self.assertFalse(result["ok"])
        self.assertTrue(result["resumed"])
        self.assertEqual(self.store.list_snapshots()["total"], 0)

    def test_hex_capacity_still_rejects_incorrect_dump_length(self):
        result = self.capture(
            self.first[:65536], output="FLASH_SNAPSHOT_KIB=0x80\nFLASH_SNAPSHOT_DUMP_OK\nFLASH_SNAPSHOT_RESUME_OK\n")
        self.assertFalse(result["ok"])
        self.assertTrue(result["resumed"])
        self.assertIn("size", result["error"])
        self.assertEqual(result["expected_bytes"], 131072)
        self.assertEqual(self.store.list_snapshots()["total"], 0)

    def test_capacity_does_not_accept_partial_numeric_marker(self):
        result = self.capture(
            self.first, output="FLASH_SNAPSHOT_KIB=0x800\nFLASH_SNAPSHOT_DUMP_OK\nFLASH_SNAPSHOT_RESUME_OK\n")
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.list_snapshots()["total"], 0)

    def test_resume_missing_is_not_saved(self):
        result = self.capture(self.first, output="FLASH_SNAPSHOT_KIB=128\nFLASH_SNAPSHOT_DUMP_OK\n")
        self.assertFalse(result["ok"])
        self.assertFalse(result["resumed"])
        self.assertEqual(self.store.list_snapshots()["total"], 0)

    def test_openocd_nonzero_is_failure(self):
        result = self.capture(self.first, exit_code=1)
        self.assertFalse(result["ok"])

    def test_short_dump_is_failure(self):
        result = self.capture(self.first[:200], kib=128)
        self.assertFalse(result["ok"])
        self.assertIn("size", result["error"])

    def test_timeout_is_failure_and_cleanup(self):
        import subprocess
        with patch("flash_snapshots.subprocess.run", side_effect=subprocess.TimeoutExpired(["openocd"], 75)):
            result = self.store.capture(["openocd"], self.work)
        self.assertFalse(result["ok"])
        self.assertFalse(result["resumed"])
        self.assertFalse(any(self.work.iterdir()))

    def test_label_validation(self):
        with self.assertRaises(SnapshotError):
            self.store.capture(["openocd"], self.work, "unsafe\nlabel")

    def test_invalid_snapshot_id_yields_clean_mcp_error(self):
        result = snapshot_tool_result(self.store.info, "<baseline ID>")
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error_type"], "SnapshotError")
        self.assertIn("32 lowercase hex", result["error"])
        self.assertEqual(snapshot_tool_result(self.store.chunk, "<baseline ID>")["ok"], False)
        self.assertEqual(snapshot_tool_result(self.store.diff, "<baseline ID>", "<control ID>")["ok"], False)

    def test_invalid_diff_id_has_precise_error(self):
        result = snapshot_tool_result(self.store.diff_chunk, "<diff ID>")
        self.assertFalse(result["ok"])
        self.assertIn("diff_id", result["error"])

    def test_unexpected_error_is_not_swallowed(self):
        def broken():
            raise RuntimeError("unexpected bug")
        with self.assertRaises(RuntimeError):
            snapshot_tool_result(broken)

    def test_ids_reject_path_traversal(self):
        with self.assertRaises(SnapshotError):
            self.store.info("../../etc/passwd")

    def test_tampered_snapshot_detected(self):
        result = self.capture(self.first)
        path = Path(result["path"])
        path.write_bytes(b"X" + path.read_bytes()[1:])
        with self.assertRaises(SnapshotError):
            self.store.info(result["snapshot_id"])

    def test_diff_groups_and_mapping_csv(self):
        a = self.capture(self.first)
        b = self.capture(bytes(self.second))
        result = self.store.diff(a["snapshot_id"], b["snapshot_id"], "filter experiment")
        self.assertTrue(result["ok"])
        self.assertEqual(result["changed_bytes"], 3)
        self.assertEqual(result["range_count"], 2)
        self.assertEqual(result["ranges"][0]["address_start"], "0x08013843")
        self.assertEqual(result["changed_pages"][0]["page"], 78)
        with Path(result["csv_path"]).open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["new_hex"], "01")
        self.assertEqual(rows[0]["aligned_word_address"], "0x08013840")
        self.assertEqual(len(json.loads(Path(result["json_path"]).read_text())["ranges"]), 2)

    def test_same_image_no_diffs(self):
        a = self.capture(self.first)
        result = self.store.diff(a["snapshot_id"], a["snapshot_id"])
        self.assertEqual(result["changed_bytes"], 0)
        self.assertEqual(result["range_count"], 0)

    def test_mismatched_size_rejected(self):
        a = self.capture(self.first)
        b = self.capture(self.first[:65536], kib=64)
        with self.assertRaises(SnapshotError):
            self.store.diff(a["snapshot_id"], b["snapshot_id"])

    def test_chunk_exact_bytes_and_sha(self):
        snap = self.capture(bytes(self.second))
        result = self.store.chunk(snap["snapshot_id"], 78*1024 + 0x40, 100)
        chunk = base64.b64decode(result["base64"])
        self.assertEqual(chunk, self.second[78*1024 + 0x40:78*1024 + 0x40 + 100])
        self.assertEqual(hashlib.sha256(chunk).hexdigest(), result["chunk_sha256"])

    def test_chunk_validates_limits(self):
        snap = self.capture(self.first)
        for offset, length in [(-1,1),(0,8193),(131072,1),(0,0)]:
            with self.assertRaises(SnapshotError):
                self.store.chunk(snap["snapshot_id"], offset, length)

    def test_diff_report_chunks_allow_mcp_download(self):
        a = self.capture(self.first)
        b = self.capture(bytes(self.second))
        report = self.store.diff(a["snapshot_id"], b["snapshot_id"])
        for kind in ("csv", "json"):
            result = self.store.diff_chunk(report["diff_id"], kind, 0, 128)
            data = Path(report[f"{kind}_path"]).read_bytes()
            self.assertEqual(base64.b64decode(result["base64"]), data[:128])
            self.assertEqual(result["report_sha256"], hashlib.sha256(data).hexdigest())

    def test_diff_report_rejects_invalid_ids_and_formats(self):
        with self.assertRaises(SnapshotError):
            self.store.diff_chunk("../etc/passwd")
        with self.assertRaises(SnapshotError):
            self.store.diff_chunk("0" * 32, "txt")

    def test_unchanged_same_and_packed_integer_ranges(self):
        self.assertEqual(analyze_difference(b"\x01\x02", b"\x01\x02")["changed_bytes"], 0)
        with self.assertRaises(SnapshotError):
            analyze_difference(b"abc", b"ab")


if __name__ == "__main__":
    unittest.main()
