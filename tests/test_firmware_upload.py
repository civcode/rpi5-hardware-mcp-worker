"""Offline, dependency-free tests for bounded MCP firmware upload sessions."""
import base64
import hashlib
from pathlib import Path
import tempfile
import unittest

from firmware_upload import FirmwareUploadManager, UploadError, MAX_CHUNK_BYTES


def hex_record(kind, addr, data=b""):
    body = bytes([len(data), addr >> 8, addr & 255, kind]) + data
    return b":" + (body + bytes([(-sum(body)) & 255])).hex().upper().encode() + b"\n"


def full_size_hex():
    records = [hex_record(4, 0, b"\x08\x00")]
    for i in range(4902):
        address = i * 16
        if address >= 65536 and address - 16 < 65536:
            records.append(hex_record(4, 0, b"\x08\x01"))
        records.append(hex_record(0, address & 0xFFFF, bytes([i % 256]) * 16))
    records.append(hex_record(1, 0))
    return b"".join(records)


def inspect(data):
    if not data.endswith(b":00000001FF\n"):
        raise UploadError("HEX_MISSING_EOF", "HEX missing EOF", decoded_bytes=len(data))
    return {"start": "0x08000000", "end_exclusive": "0x08013260", "data_bytes": 4902 * 16}


class FirmwareUploadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.manager = FirmwareUploadManager(self.root, inspect, 2 * 1024 * 1024)

    def tearDown(self):
        self.manager.close()
        self.directory.cleanup()

    def upload(self, data, sha=None):
        sha = sha or hashlib.sha256(data).hexdigest()
        session = self.manager.begin(len(data), sha)
        uid = session["upload_id"]
        last = None
        for offset in range(0, len(data), MAX_CHUNK_BYTES):
            part = base64.b64encode(data[offset:offset + MAX_CHUNK_BYTES]).decode()
            result = self.manager.chunk(uid, offset, part)
            self.assertEqual(result["next_offset"], min(offset + MAX_CHUNK_BYTES, len(data)))
            last = (offset, part)
        return uid, sha, last

    def test_full_realistic_220k_upload_and_retry(self):
        data = full_size_hex()
        self.assertGreater(len(data), 200_000)
        uid, sha, (offset, part) = self.upload(data)
        self.assertTrue(self.manager.chunk(uid, offset, part)["retry"])
        artifact = self.manager.finalize(uid)
        self.assertEqual(artifact["sha256"], sha)
        self.assertEqual(artifact["uploaded_bytes"], len(data))
        self.assertEqual(artifact["path"].read_bytes(), data)
        self.assertEqual({p.suffix for p in self.root.iterdir()}, {".hex"})

    def test_reject_out_of_order_and_conflicting_duplicate(self):
        data = full_size_hex()
        uid = self.manager.begin(len(data), hashlib.sha256(data).hexdigest())["upload_id"]
        with self.assertRaises(UploadError) as err:
            self.manager.chunk(uid, 16, base64.b64encode(data[16:32]).decode())
        self.assertEqual(err.exception.code, "CHUNK_OUT_OF_ORDER")
        first = base64.b64encode(data[:32]).decode()
        self.manager.chunk(uid, 0, first)
        with self.assertRaises(UploadError) as err:
            self.manager.chunk(uid, 0, base64.b64encode(b"Z" * 32).decode())
        self.assertEqual(err.exception.code, "CHUNK_CONFLICT")
        self.assertTrue(self.manager.chunk(uid, 0, first)["retry"])

    def test_interrupted_upload_and_abort(self):
        data = full_size_hex()
        uid = self.manager.begin(len(data), hashlib.sha256(data).hexdigest())["upload_id"]
        self.manager.chunk(uid, 0, base64.b64encode(data[:100]).decode())
        with self.assertRaises(UploadError) as err:
            self.manager.finalize(uid)
        self.assertEqual(err.exception.code, "UPLOAD_INCOMPLETE")
        self.assertEqual(err.exception.details["next_offset"], 100)
        self.assertTrue(self.manager.abort(uid)["aborted"])
        self.assertFalse(list(self.root.glob("*.part")))

    def test_sha256_mismatch(self):
        data = full_size_hex()
        uid, _, _ = self.upload(data, "0" * 64)
        with self.assertRaises(UploadError) as err:
            self.manager.finalize(uid)
        self.assertEqual(err.exception.code, "UPLOAD_SHA256_MISMATCH")
        self.assertEqual(err.exception.details["actual_sha256"], hashlib.sha256(data).hexdigest())

    def test_restart_invalidates_upload_id(self):
        uid = self.manager.begin(10, "1" * 64)["upload_id"]
        self.manager.close()
        reloaded = FirmwareUploadManager(self.root, inspect, 2 * 1024 * 1024)
        with self.assertRaises(UploadError) as err:
            reloaded.chunk(uid, 0, base64.b64encode(b"test").decode())
        self.assertEqual(err.exception.code, "UPLOAD_UNKNOWN")

    def test_invalid_size_sha_and_base64(self):
        with self.assertRaises(UploadError) as err:
            self.manager.begin(0, "0" * 64)
        self.assertEqual(err.exception.code, "UPLOAD_SIZE_INVALID")
        with self.assertRaises(UploadError) as err:
            self.manager.begin(50, "not-a-sha")
        self.assertEqual(err.exception.code, "SHA256_INVALID")
        uid = self.manager.begin(3, hashlib.sha256(b"bad").hexdigest())["upload_id"]
        with self.assertRaises(UploadError) as err:
            self.manager.chunk(uid, 0, "not-base64!")
        self.assertEqual(err.exception.code, "CHUNK_BASE64_INVALID")
        self.manager.chunk(uid, 0, base64.b64encode(b"bad").decode())
        with self.assertRaises(UploadError) as err:
            self.manager.finalize(uid)
        self.assertEqual(err.exception.code, "HEX_MISSING_EOF")
        self.manager.abort(uid)

    def test_at_most_four_open_sessions(self):
        ids = [self.manager.begin(10, "b" * 64)["upload_id"] for _ in range(4)]
        with self.assertRaises(UploadError) as err:
            self.manager.begin(10, "a" * 64)
        self.assertEqual(err.exception.code, "UPLOAD_LIMIT")
        for uid in ids:
            self.manager.abort(uid)


if __name__ == "__main__":
    unittest.main()
