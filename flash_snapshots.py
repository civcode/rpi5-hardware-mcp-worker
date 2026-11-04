"""Bounded, read-only STM32F103 flash snapshots and offline byte-level diffs.

A snapshot halts the target temporarily.  It never resets/programs/erases the
MCU and always attempts an explicit resume, including on a capture error.
Stored snapshots survive MCP worker restarts; all tool inputs are IDs, not paths.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import uuid


FLASH_START = 0x08000000
ALLOWED_FLASH_KIB = (64, 128)
PAGE_BYTES = 1024
_ID = re.compile(r"[0-9a-f]{32}\Z")
_KIB = re.compile(r"FLASH_SNAPSHOT_KIB=(64|128)\b")
_DUMP_OK = "FLASH_SNAPSHOT_DUMP_OK"
_RESUME_OK = "FLASH_SNAPSHOT_RESUME_OK"
_MAX_LOG = 8000


class SnapshotError(ValueError):
    pass


def snapshot_tool_result(operation, *args, **kwargs) -> dict:
    """Report expected snapshot input/file errors as normal MCP tool results.

    Keep unexpected programming errors visible; only handle the domain's
    validation and filesystem errors here.
    """
    try:
        return operation(*args, **kwargs)
    except (SnapshotError, OSError) as exc:
        return {"ok": False, "error": str(exc),
                "error_type": type(exc).__name__}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_id(value: str, name: str = "snapshot_id") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SnapshotError(f"{name} must be 32 lowercase hex characters")
    return value


def _candidate_numbers(data: bytes, offset: int) -> dict:
    """Little-endian interpretations starting at a changed byte's aligned word."""
    result = {"u8": data[offset]}
    if offset + 2 <= len(data):
        result["u16_le"] = int.from_bytes(data[offset:offset + 2], "little")
    if offset + 4 <= len(data):
        word = data[offset:offset + 4]
        result["u32_le"] = int.from_bytes(word, "little")
        f32 = struct.unpack("<f", word)[0]
        result["f32_le"] = f32 if math.isfinite(f32) else None
    return result


def analyze_difference(before: bytes, after: bytes, start: int = FLASH_START) -> dict:
    """Pure offline comparison: contiguous byte ranges and 1-KiB page counts."""
    if len(before) != len(after) or not before:
        raise SnapshotError("Snapshots must have equal non-zero byte lengths")
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    runs = []
    for index in changed:
        if not runs or index != runs[-1][1]:
            runs.append([index, index + 1])
        else:
            runs[-1][1] += 1
    pages = {}
    for index in changed:
        page = index // PAGE_BYTES
        pages[page] = pages.get(page, 0) + 1
    return {
        "flash_start": f"0x{start:08X}",
        "length": len(before),
        "changed_bytes": len(changed),
        "changed_pages": [
            {"page": page, "address": f"0x{start + page * PAGE_BYTES:08X}",
             "changed_bytes": count}
            for page, count in sorted(pages.items())
        ],
        "ranges": [
            {"address_start": f"0x{start + a:08X}",
             "address_end_exclusive": f"0x{start + b:08X}",
             "offset": a, "length": b - a,
             "old_hex": before[a:min(b, a + 16)].hex(),
             "new_hex": after[a:min(b, a + 16)].hex(),
             "preview_truncated": b - a > 16}
            for a, b in runs
        ],
    }


def _write_diff_csv(path: Path, before: bytes, after: bytes, start: int) -> None:
    """One row per changed byte, with aligned candidate 16/32-bit/float views."""
    with path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["address", "offset", "old_hex", "new_hex", "aligned_word_address",
                         "old_u16_le", "new_u16_le", "old_u32_le", "new_u32_le",
                         "old_f32_le", "new_f32_le"])
        for i, (a, b) in enumerate(zip(before, after)):
            if a == b:
                continue
            # Word-aligned views are hypotheses, not proven field boundaries.
            aligned = i & ~3
            old = _candidate_numbers(before, aligned)
            new = _candidate_numbers(after, aligned)
            writer.writerow([
                f"0x{start + i:08X}", i, f"{a:02X}", f"{b:02X}",
                f"0x{start + aligned:08X}", old.get("u16_le", ""), new.get("u16_le", ""),
                old.get("u32_le", ""), new.get("u32_le", ""),
                old.get("f32_le", ""), new.get("f32_le", ""),
            ])


class FlashSnapshotStore:
    """Persistent snapshot store and bounded OpenOCD capture.

    Does not own the OpenOCD probe lock; the caller holds OpenOCDWorker._lock
    and must reject simultaneous worker-managed GDB sessions.
    """

    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory) if directory else Path(
            os.environ.get("RPI5_FLASH_SNAPSHOT_DIR", str(Path.home() / "rpi5-flash-snapshots"))
        ).expanduser()

    def _paths(self, snapshot_id: str) -> tuple[Path, Path]:
        stem = _check_id(snapshot_id)
        return self.directory / f"{stem}.bin", self.directory / f"{stem}.json"

    def _read(self, snapshot_id: str) -> tuple[bytes, dict]:
        binary, metadata = self._paths(snapshot_id)
        try:
            info = json.loads(metadata.read_text(encoding="utf-8"))
            data = binary.read_bytes()
        except (OSError, ValueError) as exc:
            raise SnapshotError(f"Snapshot unavailable: {snapshot_id}") from exc
        if (info.get("snapshot_id") != snapshot_id or
                info.get("flash_start") != f"0x{FLASH_START:08X}" or
                info.get("flash_kib") not in ALLOWED_FLASH_KIB or
                info.get("length") != info["flash_kib"] * 1024 or
                len(data) != info["length"] or _sha(data) != info.get("sha256")):
            raise SnapshotError(f"Snapshot failed integrity verification: {snapshot_id}")
        return data, info

    def info(self, snapshot_id: str) -> dict:
        _, info = self._read(snapshot_id)
        return {"ok": True, **info, "path": str(self._paths(snapshot_id)[0])}

    def list_snapshots(self) -> dict:
        found = []
        if self.directory.exists():
            for path in sorted(self.directory.glob("*.json")):
                if not _ID.fullmatch(path.stem):
                    continue
                try:
                    found.append(self.info(path.stem))
                except (SnapshotError, OSError):
                    continue
        found.sort(key=lambda item: item.get("captured_utc", ""))
        return {"ok": True, "snapshots": found[-100:], "total": len(found),
                "directory": str(self.directory)}

    def capture(self, openocd_args: list[str], workspace: Path,
                label: str = "", timeout: int = 75) -> dict:
        if not isinstance(label, str) or len(label) > 100 or any(c in label for c in "\r\n\x00"):
            raise SnapshotError("label must be 0-100 characters without control characters")
        snapshot_id = uuid.uuid4().hex
        temp_path = workspace / f"snapshot-{snapshot_id}.bin"
        # The Tcl path is generated from an internal temp dir, never from user input.
        # Reject braces/newlines to avoid interpreting a path as Tcl instructions.
        path_text = str(temp_path.resolve())
        if any(c in path_text for c in "{}\r\n\\"):
            raise SnapshotError("Temporary capture path cannot be represented safely in Tcl")
        tcl = (
            "set snap_rc [catch { "
            "halt; "
            "set snap_kib [lindex [read_memory 0x1FFFF7E0 16 1] 0]; "
            "if {$snap_kib != 64 && $snap_kib != 128} {error \"unsupported STM32 flash capacity\"}; "
            "puts \"FLASH_SNAPSHOT_KIB=$snap_kib\"; "
            f"dump_image {{{path_text}}} 0x08000000 [expr {{$snap_kib * 1024}}]; "
            "puts \"FLASH_SNAPSHOT_DUMP_OK\" "
            "} snap_error]; "
            "set resume_rc [catch {resume} resume_error]; "
            "if {$resume_rc} {puts \"FLASH_SNAPSHOT_RESUME_ERROR=$resume_error\"} "
            "else {puts \"FLASH_SNAPSHOT_RESUME_OK\"}; "
            "if {$snap_rc} {puts \"FLASH_SNAPSHOT_ERROR=$snap_error\"}; "
            "if {$snap_rc || $resume_rc} {shutdown error} else {shutdown}"
        )
        command = list(openocd_args) + ["-c", "gdb_port disabled", "-c", "telnet_port disabled",
                                       "-c", "tcl_port disabled", "-c", "init", "-c", tcl]
        output = ""
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", check=False, timeout=timeout)
            output = result.stdout
            match = _KIB.search(output)
            kib = int(match.group(1)) if match else None
            if (result.returncode != 0 or kib not in ALLOWED_FLASH_KIB or
                    _DUMP_OK not in output or _RESUME_OK not in output):
                return {"ok": False, "error": "OpenOCD capture or explicit resume failed",
                        "exit_code": result.returncode, "resumed": _RESUME_OK in output,
                        "output": output[-_MAX_LOG:]}
            data = temp_path.read_bytes()
            if len(data) != kib * 1024:
                return {"ok": False, "error": "Flash dump size does not match detected flash capacity",
                        "actual_bytes": len(data), "expected_bytes": kib * 1024,
                        "resumed": True, "output": output[-_MAX_LOG:]}
            self.directory.mkdir(parents=True, exist_ok=True)
            binary, metadata = self._paths(snapshot_id)
            binary.write_bytes(data)
            info = {"snapshot_id": snapshot_id, "label": label,
                    "captured_utc": datetime.now(timezone.utc).isoformat(),
                    "flash_start": f"0x{FLASH_START:08X}", "flash_kib": kib,
                    "length": len(data), "sha256": _sha(data),
                    "resumed": True}
            metadata.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
            return {"ok": True, **info, "path": str(binary),
                    "output": output[-_MAX_LOG:],
                    "note": "SWD halt/resume interrupts firmware execution; no flash programming or reset was requested."}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc), "resumed": False,
                    "warning": "Resume not confirmed. Inspect target execution before further operations."}
        finally:
            temp_path.unlink(missing_ok=True)

    def diff(self, before_id: str, after_id: str, label: str = "") -> dict:
        if not isinstance(label, str) or len(label) > 100 or any(c in label for c in "\r\n\x00"):
            raise SnapshotError("label must be 0-100 characters without control characters")
        before, before_info = self._read(before_id)
        after, after_info = self._read(after_id)
        if before_info["flash_kib"] != after_info["flash_kib"]:
            raise SnapshotError("Flash capacity differs between the snapshots")
        report = analyze_difference(before, after)
        diff_id = uuid.uuid4().hex
        self.directory.mkdir(parents=True, exist_ok=True)
        csv_path = self.directory / f"diff-{diff_id}.csv"
        json_path = self.directory / f"diff-{diff_id}.json"
        _write_diff_csv(csv_path, before, after, FLASH_START)
        full = {"diff_id": diff_id, "label": label, "before_id": before_id,
                "after_id": after_id, "before_sha256": before_info["sha256"],
                "after_sha256": after_info["sha256"], **report}
        json_path.write_text(json.dumps(full, indent=2) + "\n", encoding="utf-8")
        return {"ok": True, **{k: v for k, v in full.items() if k != "ranges"},
                "range_count": len(report["ranges"]), "ranges": report["ranges"][:64],
                "ranges_truncated": len(report["ranges"]) > 64,
                "csv_path": str(csv_path), "json_path": str(json_path),
                "note": "Numeric interpretations in CSV are candidates, not verified field types."}

    def chunk(self, snapshot_id: str, offset: int = 0, length: int = 4096) -> dict:
        """Retrieve a bounded snapshot segment for offline analysis over MCP."""
        import base64
        data, info = self._read(snapshot_id)
        if type(offset) is not int or offset < 0 or offset >= len(data):
            raise SnapshotError("offset must be within the captured flash image")
        if type(length) is not int or not 1 <= length <= 8192:
            raise SnapshotError("length must be between 1 and 8192")
        piece = data[offset:offset + length]
        return {"ok": True, "snapshot_id": snapshot_id, "offset": offset,
                "bytes": len(piece), "chunk_sha256": _sha(piece),
                "snapshot_sha256": info["sha256"], "total_bytes": len(data),
                "base64": base64.b64encode(piece).decode("ascii")}

    def diff_chunk(self, diff_id: str, format: str = "csv",
                   offset: int = 0, length: int = 4096) -> dict:
        """Retrieve a bounded stored CSV or JSON diff report, with integrity hash."""
        import base64
        _check_id(diff_id, "diff_id")
        if format not in ("csv", "json"):
            raise SnapshotError("format must be csv or json")
        if type(offset) is not int or offset < 0:
            raise SnapshotError("offset must be non-negative")
        if type(length) is not int or not 1 <= length <= 8192:
            raise SnapshotError("length must be between 1 and 8192")
        path = self.directory / f"diff-{diff_id}.{format}"
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SnapshotError(f"Diff report unavailable: {diff_id}") from exc
        if offset >= len(data):
            raise SnapshotError("offset exceeds diff report length")
        piece = data[offset:offset + length]
        return {"ok": True, "diff_id": diff_id, "format": format,
                "offset": offset, "bytes": len(piece), "total_bytes": len(data),
                "report_sha256": _sha(data), "chunk_sha256": _sha(piece),
                "base64": base64.b64encode(piece).decode("ascii")}


def main(argv: list[str] | None = None) -> int:
    """Offline comparison of existing raw binary backups; no hardware involved."""
    import argparse
    parser = argparse.ArgumentParser(description="Compare STM32 raw main-flash .bin backups offline")
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--csv", type=Path, help="write changed-byte CSV")
    parser.add_argument("--json", type=Path, help="write full range JSON")
    args = parser.parse_args(argv)
    before = args.before.read_bytes()
    after = args.after.read_bytes()
    if len(before) not in tuple(k * 1024 for k in ALLOWED_FLASH_KIB):
        parser.error("expected a complete 64- or 128-KiB STM32 main-flash image")
    report = analyze_difference(before, after)
    report["before_sha256"] = _sha(before)
    report["after_sha256"] = _sha(after)
    if args.csv:
        _write_diff_csv(args.csv, before, after, FLASH_START)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"changed_bytes": report["changed_bytes"],
                      "range_count": len(report["ranges"]),
                      "changed_pages": report["changed_pages"],
                      "first_64_ranges": report["ranges"][:64],
                      "before_sha256": report["before_sha256"],
                      "after_sha256": report["after_sha256"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
