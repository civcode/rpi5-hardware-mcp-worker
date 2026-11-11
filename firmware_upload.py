"""Bounded, host-only Intel HEX transfer sessions for the Pi MCP worker.

Only staged data is touched. The target is never accessed by this module.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path
import re
import threading
import uuid
from typing import Callable

MAX_CHUNK_BYTES = 8192
MAX_ACTIVE_UPLOADS = 4


class UploadError(ValueError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def result(self) -> dict:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "details": self.details}}


class FirmwareUploadManager:
    def __init__(self, workspace: Path, inspect_hex: Callable[[bytes], dict], max_hex_bytes: int):
        self.workspace = Path(workspace)
        self.inspect_hex = inspect_hex
        self.max_hex_bytes = max_hex_bytes
        self._uploads: dict[str, dict] = {}
        self._lock = threading.RLock()

    def begin(self, total_bytes: int, expected_sha256: str) -> dict:
        if type(total_bytes) is not int or not 0 < total_bytes <= self.max_hex_bytes:
            raise UploadError("UPLOAD_SIZE_INVALID", "Upload size must be between 1 and the configured HEX limit",
                              maximum_bytes=self.max_hex_bytes)
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise UploadError("SHA256_INVALID", "Provide the SHA-256 of the original HEX file (64 hex characters)")
        with self._lock:
            if len(self._uploads) >= MAX_ACTIVE_UPLOADS:
                raise UploadError("UPLOAD_LIMIT", "Too many open uploads; abort an unused upload first",
                                  maximum_sessions=MAX_ACTIVE_UPLOADS)
            upload_id = uuid.uuid4().hex
            path = self.workspace / f"{upload_id}.part"
            with path.open("xb"):
                pass
            self._uploads[upload_id] = {"path": path, "total": total_bytes,
                                        "sha256": expected_sha256.lower(), "offset": 0}
            return {"ok": True, "upload_id": upload_id, "total_bytes": total_bytes,
                    "next_offset": 0, "max_chunk_bytes": MAX_CHUNK_BYTES}

    def chunk(self, upload_id: str, offset: int, chunk_base64: str) -> dict:
        if type(offset) is not int or offset < 0:
            raise UploadError("CHUNK_OFFSET_INVALID", "Chunk offset must be a nonnegative integer")
        if not isinstance(chunk_base64, str) or len(chunk_base64) > (MAX_CHUNK_BYTES * 4 // 3 + 4):
            raise UploadError("CHUNK_SIZE_INVALID", "Base64 chunk exceeds the permitted chunk limit",
                              max_chunk_bytes=MAX_CHUNK_BYTES)
        try:
            chunk = base64.b64decode(chunk_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise UploadError("CHUNK_BASE64_INVALID", "Chunk must contain Base64-encoded file bytes") from exc
        if not chunk or len(chunk) > MAX_CHUNK_BYTES:
            raise UploadError("CHUNK_SIZE_INVALID", "Decoded chunk must contain 1 to 8192 bytes",
                              max_chunk_bytes=MAX_CHUNK_BYTES)
        with self._lock:
            item = self._uploads.get(upload_id)
            if item is None:
                raise UploadError("UPLOAD_UNKNOWN", "Upload ID not found; start a new upload after a worker restart")
            expected = item["offset"]
            if offset < expected and offset + len(chunk) <= expected:
                with item["path"].open("rb") as f:
                    f.seek(offset)
                    previous = f.read(len(chunk))
                if previous != chunk:
                    raise UploadError("CHUNK_CONFLICT", "Retry data differs from the previously uploaded bytes",
                                      next_offset=expected)
                return {"ok": True, "upload_id": upload_id, "next_offset": expected, "retry": True}
            if offset != expected:
                raise UploadError("CHUNK_OUT_OF_ORDER", "Send chunks in increasing byte-offset order",
                                  next_offset=expected, received_offset=offset)
            if offset + len(chunk) > item["total"]:
                raise UploadError("CHUNK_EXCEEDS_SIZE", "Chunk would exceed declared file length",
                                  total_bytes=item["total"], next_offset=expected)
            with item["path"].open("ab") as f:
                f.write(chunk)
            item["offset"] += len(chunk)
            return {"ok": True, "upload_id": upload_id, "next_offset": item["offset"],
                    "remaining_bytes": item["total"] - item["offset"], "retry": False}

    def finalize(self, upload_id: str) -> dict:
        with self._lock:
            item = self._uploads.get(upload_id)
            if item is None:
                raise UploadError("UPLOAD_UNKNOWN", "Upload ID not found; start a new upload after a worker restart")
            if item["offset"] != item["total"]:
                raise UploadError("UPLOAD_INCOMPLETE", "Upload incomplete; send the remaining chunks",
                                  received_bytes=item["offset"], expected_bytes=item["total"],
                                  next_offset=item["offset"])
            contents = item["path"].read_bytes()
            if len(contents) != item["total"]:
                raise UploadError("UPLOAD_LENGTH_MISMATCH", "Staged file length changed unexpectedly",
                                  received_bytes=len(contents), expected_bytes=item["total"])
            digest = hashlib.sha256(contents).hexdigest()
            if digest != item["sha256"]:
                raise UploadError("UPLOAD_SHA256_MISMATCH", "Staged HEX differs from the declared original file",
                                  expected_sha256=item["sha256"], actual_sha256=digest)
            metadata = self.inspect_hex(contents)
            artifact_id = uuid.uuid4().hex
            final_path = self.workspace / f"{artifact_id}.hex"
            os.replace(item["path"], final_path)
            del self._uploads[upload_id]
            return {"ok": True, "artifact_id": artifact_id, "path": final_path,
                    "sha256": digest, "uploaded_bytes": len(contents), **metadata}

    def abort(self, upload_id: str) -> dict:
        with self._lock:
            item = self._uploads.pop(upload_id, None)
            if item is None:
                raise UploadError("UPLOAD_UNKNOWN", "Upload ID not found or already finalized")
            item["path"].unlink(missing_ok=True)
            return {"ok": True, "aborted": True, "upload_id": upload_id}

    def close(self) -> None:
        with self._lock:
            for item in self._uploads.values():
                item["path"].unlink(missing_ok=True)
            self._uploads.clear()
