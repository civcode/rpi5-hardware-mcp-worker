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

    def _read(self, identifier):
        binary, record = self._paths(identifier)
        info = json.loads(record.read_text(encoding="utf-8"))
        data = binary.read_bytes()
        if (info.get("artifact_id") != identifier or info.get("length") != len(data)
                or info.get("sha256") != hashlib.sha256(data).hexdigest()):
            raise ValueError("Artifact failed integrity verification")
        return info, data

    def info(self, identifier):
        with self._lock:
            info, _ = self._read(identifier)
            return {"ok": True, **info}

    def chunk(self, identifier, offset=0, length=8192):
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 8192:
            raise ValueError("offset >= 0; length 1..8192 bytes")
        with self._lock:
            info, data = self._read(identifier)
            if offset > len(data):
                raise ValueError("offset exceeds artifact length")
            chunk = data[offset:offset+length]
            return {"ok": True, "artifact_id": identifier, "offset": offset,
                    "length": len(chunk), "total_bytes": info["length"],
                    "next_offset": offset + len(chunk), "eof": offset + len(chunk) == len(data),
                    "data_base64": base64.b64encode(chunk).decode("ascii"),
                    "chunk_sha256": hashlib.sha256(chunk).hexdigest(), "sha256": info["sha256"]}

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
            exists = binary.exists() or record.exists()
            binary.unlink(missing_ok=True)
            record.unlink(missing_ok=True)
            return {"ok": True, "deleted": exists, "artifact_id": identifier}


def artifact_result(fn, *args):
    try:
        return fn(*args)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
