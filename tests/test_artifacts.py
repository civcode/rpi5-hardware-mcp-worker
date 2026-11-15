import base64
import hashlib
from pathlib import Path
import tempfile
import unittest
from artifacts import ArtifactStore


class ArtifactTests(unittest.TestCase):
    def test_roundtrip_and_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            data = bytes(range(256)) * 50
            aid = store.save(data, "test")["artifact_id"]
            again = ArtifactStore(directory)
            self.assertTrue(again.info(aid)["whole_sha256_verified"])
            one = again.chunk(aid)
            two = again.chunk(aid, one["next_offset"])
            self.assertTrue(two["eof"])
            self.assertEqual(base64.b64decode(one["data_base64"]) +
                             base64.b64decode(two["data_base64"]), data)
            self.assertEqual(two["sha256"], hashlib.sha256(data).hexdigest())
            (Path(directory) / (aid + ".bin")).write_bytes(b"changed")
            with self.assertRaises(ValueError):
                again.info(aid)
            self.assertTrue(again.delete(aid)["deleted"])

    def test_live_prefix_finalize_and_abandoned_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            aid = store.begin_stream("gdb")
            store.append_stream(aid, b"one\n")
            self.assertFalse(store.info(aid)["finalized"])
            self.assertIsNone(store.chunk(aid)["sha256"])
            with self.assertRaises(ValueError):
                store.delete(aid)
            store.append_stream(aid, b"two\n")
            store.finalize_stream(aid)
            self.assertTrue(store.info(aid)["whole_sha256_verified"])
            self.assertEqual(store.info(aid)["length"], 8)
            orphan = store.begin_stream("gdb")
            self.assertTrue(ArtifactStore(directory).delete(orphan)["deleted"])

    def test_empty_and_bad_range(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            with self.assertRaises(ValueError):
                store.info("../../etc/passwd")
            aid = store.save(b"", "test")["artifact_id"]
            self.assertTrue(store.chunk(aid)["eof"])
            with self.assertRaises(ValueError):
                store.chunk(aid, 1)
