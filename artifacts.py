"""Persistent binary evidence shared by the hardware tools."""
from __future__ import annotations
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ArtifactStore:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.getenv("RPI5_ARTIFACT_DIR",
            str(Path.home() / "rpi5-hardware-artifacts"))).expanduser()
        self._lock = threading.RLock()
        self._active_streams = set()

    def _paths(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Use the returned 32-character artifact_id")
        return self.directory / (identifier + ".bin"), self.directory / (identifier + ".json")

    def save(self, data, kind, metadata=None):
        identifier = uuid.uuid4().hex
        info = {"artifact_id": identifier, "kind": kind, "created_utc": utc_now(),
                "length": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "metadata": metadata or {}}
        with self._lock:
            binary, record = self._paths(identifier)
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                binary.write_bytes(data)
                record.write_text(json.dumps(info) + "\n", encoding="utf-8")
            except OSError:
                binary.unlink(missing_ok=True)
                record.unlink(missing_ok=True)
                raise
        return info

    def begin_stream(self, kind, metadata=None):
        """Create a persistent live transcript. A crash leaves it unfinalized."""
        identifier = uuid.uuid4().hex
        info = {"artifact_id": identifier, "kind": kind, "created_utc": utc_now(),
                "finalized": False, "metadata": metadata or {}}
        with self._lock:
            binary, record = self._paths(identifier)
            self.directory.mkdir(parents=True, exist_ok=True)
            binary.touch(exist_ok=False)
            try:
                record.write_text(json.dumps(info) + "\n", encoding="utf-8")
            except OSError:
                binary.unlink(missing_ok=True)
                record.unlink(missing_ok=True)
                raise
            self._active_streams.add(identifier)
        return identifier

    def append_stream(self, identifier, data):
        with self._lock:
            binary, _ = self._paths(identifier)
            if identifier not in self._active_streams:
                raise ValueError("Transcript is not active")
            with binary.open("ab") as stream:
                stream.write(data)

    def finalize_stream(self, identifier, truncated=False):
        with self._lock:
            binary, record = self._paths(identifier)
            info = json.loads(record.read_text(encoding="utf-8"))
            try:
                size, digest = self._hash_file(binary)
                info.update(finalized=True, length=size, truncated=truncated, sha256=digest)
                temporary = record.with_suffix(".tmp")
                temporary.write_text(json.dumps(info) + "\n", encoding="utf-8")
                temporary.replace(record)
            finally:
                self._active_streams.discard(identifier)

    @staticmethod
    def _hash_file(binary):
        digest = hashlib.sha256()
        size = 0
        with binary.open("rb") as stream:
            for piece in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(piece)
                digest.update(piece)
        return size, digest.hexdigest()

    def _metadata(self, identifier):
        binary, record = self._paths(identifier)
        info = json.loads(record.read_text(encoding="utf-8"))
        if info.get("artifact_id") != identifier:
            raise ValueError("Artifact metadata ID mismatch")
        return binary, info

    def info(self, identifier):
        with self._lock:
            binary, info = self._metadata(identifier)
            size, digest = self._hash_file(binary)
            if info.get("finalized", True):
                if info.get("length") != size or info.get("sha256") != digest:
                    raise ValueError("Artifact failed integrity verification")
                return {"ok": True, **info, "finalized": True, "whole_sha256_verified": True}
            return {"ok": True, **info, "length": size, "sha256": digest,
                    "active": identifier in self._active_streams, "whole_sha256_verified": False}

    def chunk(self, identifier, offset=0, length=8192):
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 8192:
            raise ValueError("offset >= 0; length 1..8192 bytes")
        with self._lock:
            binary, info = self._metadata(identifier)
            finalized = info.get("finalized", True)
            with binary.open("rb") as stream:
                total = os.fstat(stream.fileno()).st_size
                if finalized and total != info.get("length"):
                    raise ValueError("Artifact length mismatch")
                if offset > total:
                    raise ValueError("offset exceeds artifact length")
                stream.seek(offset)
                chunk = stream.read(min(length, total - offset))
            return {"ok": True, "artifact_id": identifier, "offset": offset,
                    "length": len(chunk), "total_bytes": total,
                    "next_offset": offset + len(chunk), "eof": offset + len(chunk) == total,
                    "data_base64": base64.b64encode(chunk).decode("ascii"),
                    "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
                    "sha256": info.get("sha256") if finalized else None, "finalized": finalized,
                    "whole_sha256_verified": False, "truncated": info.get("truncated", False)}

    def list(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        with self._lock:
            paths = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            items = []
            for path in paths[:limit]:
                try:
                    info = json.loads(path.read_text(encoding="utf-8"))
                    if info.get("artifact_id") == path.stem:
                        items.append(info)
                except (OSError, ValueError):
                    continue
            return {"ok": True, "total": len(paths), "artifacts": items}

    def delete(self, identifier):
        with self._lock:
            binary, record = self._paths(identifier)
            if identifier in self._active_streams:
                raise ValueError("Active transcript; stop its GDB session before deletion")
            exists = binary.exists() or record.exists()
            binary.unlink(missing_ok=True)
            record.unlink(missing_ok=True)
            return {"ok": True, "deleted": exists, "artifact_id": identifier}


def artifact_result(fn, *args):
    try:
        return fn(*args)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
