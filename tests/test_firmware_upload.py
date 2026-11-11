"""Offline host-only tests for bounded MCP firmware upload sessions."""
import base64
import hashlib

import pytest

from firmware_upload import FirmwareUploadManager, UploadError, MAX_CHUNK_BYTES


def hex_record(kind, addr, data=b""):
    body = bytes([len(data), addr >> 8, addr & 255, kind]) + data
    return b":" + (body + bytes([(-sum(body)) & 255])).hex().upper().encode() + b"\n"


def valid_hex():
    # 4,902 data records, comparable to the real 220-KB firmware's record count.
    records = [hex_record(4, 0, b"\x08\x00")]
    for i in range(4902):
        address = i * 16
        if address >= 65536 and address - 16 < 65536:
            records.append(hex_record(4, 0, b"\x08\x01"))
        records.append(hex_record(0, address & 0xFFFF, bytes([i % 256]) * 16))
    records.append(hex_record(1, 0))
    return b"".join(records)


def inspect(data):
    assert data.endswith(b":00000001FF\n"), "HEX missing EOF"
    assert b":10000000" in data
    return {"start": "0x08000000", "end_exclusive": "0x08013260", "data_bytes": 4902 * 16}


def manager(tmp_path):
    return FirmwareUploadManager(tmp_path, inspect, 2 * 1024 * 1024)


def upload(m, data, chunk_size=MAX_CHUNK_BYTES):
    sha = hashlib.sha256(data).hexdigest()
    session = m.begin(len(data), sha)
    uid = session["upload_id"]
    last_payload = None
    last_offset = None
    for offset in range(0, len(data), chunk_size):
        payload = base64.b64encode(data[offset:offset + chunk_size]).decode()
        result = m.chunk(uid, offset, payload)
        assert result["next_offset"] == min(offset + chunk_size, len(data))
        last_offset, last_payload = offset, payload
    return uid, sha, last_offset, last_payload


def test_full_large_upload(tmp_path):
    m = manager(tmp_path)
    contents = valid_hex()
    assert len(contents) > 200_000
    uid, sha, offset, payload = upload(m, contents)
    assert m.chunk(uid, offset, payload)["retry"]
    complete = m.finalize(uid)
    assert complete["sha256"] == sha
    assert complete["uploaded_bytes"] == len(contents)
    assert complete["path"].read_bytes() == contents
    assert complete["artifact_id"]


def test_out_of_order_and_conflicting_retry(tmp_path):
    m = manager(tmp_path)
    data = valid_hex()
    uid = m.begin(len(data), hashlib.sha256(data).hexdigest())["upload_id"]
    with pytest.raises(UploadError, match="increasing") as e:
        m.chunk(uid, 16, base64.b64encode(data[16:32]).decode())
    assert e.value.code == "CHUNK_OUT_OF_ORDER"
    first = base64.b64encode(data[:32]).decode()
    assert m.chunk(uid, 0, first)["next_offset"] == 32
    with pytest.raises(UploadError) as e:
        m.chunk(uid, 0, base64.b64encode(b"Z" * 32).decode())
    assert e.value.code == "CHUNK_CONFLICT"
    assert m.chunk(uid, 0, first)["retry"] is True


def test_missing_tail_and_abort(tmp_path):
    m = manager(tmp_path)
    data = valid_hex()
    uid = m.begin(len(data), hashlib.sha256(data).hexdigest())["upload_id"]
    m.chunk(uid, 0, base64.b64encode(data[:100]).decode())
    with pytest.raises(UploadError) as e:
        m.finalize(uid)
    assert e.value.code == "UPLOAD_INCOMPLETE"
    assert e.value.details["next_offset"] == 100
    assert m.abort(uid)["aborted"]
    assert not list(tmp_path.glob("*.part"))


def test_invalid_sha(tmp_path):
    m = manager(tmp_path)
    data = valid_hex()
    uid = m.begin(len(data), "0" * 64)["upload_id"]
    for offset in range(0, len(data), MAX_CHUNK_BYTES):
        m.chunk(uid, offset, base64.b64encode(data[offset:offset + MAX_CHUNK_BYTES]).decode())
    with pytest.raises(UploadError) as e:
        m.finalize(uid)
    assert e.value.code == "UPLOAD_SHA256_MISMATCH"
    assert e.value.details["actual_sha256"] == hashlib.sha256(data).hexdigest()


def test_restart_invalidates_unfinished_upload(tmp_path):
    a = manager(tmp_path)
    uid = a.begin(10, "1" * 64)["upload_id"]
    a.close()
    b = manager(tmp_path)
    with pytest.raises(UploadError) as e:
        b.chunk(uid, 0, base64.b64encode(b"hello").decode())
    assert e.value.code == "UPLOAD_UNKNOWN"


def test_limits_and_missing_eof(tmp_path):
    m = manager(tmp_path)
    with pytest.raises(UploadError) as e:
        m.begin(0, "0" * 64)
    assert e.value.code == "UPLOAD_SIZE_INVALID"
    with pytest.raises(UploadError) as e:
        m.begin(50, "not a sha")
    assert e.value.code == "SHA256_INVALID"
    uid = m.begin(3, hashlib.sha256(b"bad").hexdigest())["upload_id"]
    with pytest.raises(UploadError) as e:
        m.chunk(uid, 0, "not-base64!")
    assert e.value.code == "CHUNK_BASE64_INVALID"
    m.chunk(uid, 0, base64.b64encode(b"bad").decode())
    with pytest.raises(AssertionError, match="EOF"):
        m.finalize(uid)
    m.abort(uid)


def test_session_limit(tmp_path):
    m = manager(tmp_path)
    ids = [m.begin(10, "b" * 64)["upload_id"] for _ in range(4)]
    with pytest.raises(UploadError) as e:
        m.begin(10, "a" * 64)
    assert e.value.code == "UPLOAD_LIMIT"
    for uid in ids:
        m.abort(uid)


def test_no_target_access(tmp_path):
    # The upload manager has no OpenOCD, GDB or serial dependency.
    m = manager(tmp_path)
    data = valid_hex()
    uid, _, _, _ = upload(m, data)
    m.finalize(uid)
    assert set(p.suffix for p in tmp_path.iterdir()) == {".hex"}
