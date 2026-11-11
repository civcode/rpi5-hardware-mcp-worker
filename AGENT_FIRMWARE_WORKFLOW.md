# Agent firmware staging and flash workflow (Pi MCP worker)

**Two computers, two distinct filesystems.** The *development machine* (Oberon)
owns Git worktrees and built `Project.elf` / `Project.hex`. The *Raspberry Pi*
runs this MCP server, OpenOCD, the worker-managed GDB client, and the SWD probe.
An absolute path on Oberon is **not** a path on the Pi.

## Preferred automated staging (no hardware access)

Use the MCP **tool calls**, not a giant Base64 string pasted into a chat.
No shell access to the Pi is required. Never call `flash_hex` during staging.

1. On Oberon, read the original HEX file as **bytes**. Calculate
   `total_bytes = len(data)` and `expected_sha256 = sha256(data).hexdigest()`.
   Verify the last Intel HEX record is `:00000001FF`. Note the ELF for later
   symbol-aware debugging, but do **not** send an ELF to these HEX tools.
2. Call `begin_firmware_upload(total_bytes, expected_sha256)`.
   Record the returned `upload_id` and `max_chunk_bytes` (currently 8192).
3. Slice the **original file bytes** into chunks of at most 8192. For each,
   call `upload_firmware_chunk(upload_id, offset, chunk_base64)`, where
   `chunk_base64` is the Base64 encoding of **that byte slice only**,
   and `offset` is the zero-based byte offset of the original HEX file.
   Use `next_offset` returned by the worker before sending the next chunk.
   Duplicate identical chunks are safe to retry; conflicting or out-of-order
   chunks are rejected.
4. Call `finalize_firmware_upload(upload_id)`. This verifies the exact byte
   count and SHA-256, then performs full Intel HEX checksum, EOF and address
   validation. It creates a **Pi-local staged HEX** and returns `artifact_id`.
   On incomplete or invalid uploads, use the returned error details; call
   `abort_firmware_upload(upload_id)` if abandoning the upload.
5. Call `staged_hex_info(artifact_id)` and `flash_preflight(artifact_id)`.
   Confirm the staged SHA matches the original, verify the address range and
   check `ready_for_flash_request`. Preflight is **host-only**; it does not
   contact or halt Board A. `target_flash_kib` is therefore `null` with
   `capacity_status=NOT_PROBED_HOST_ONLY`. **`flash_hex` performs a fresh
   factory-capacity probe before programming.**
6. Only with explicit authorization and a verified recovery image, invoke
   `flash_hex(artifact_id, confirm_sha256, confirm="FLASH")`. It checks
   the preflight, probe ownership, SHA-256, capacity, then performs
   `reset halt` before programming and requests verify/reset.
   A successful staging or preflight result **does not authorize flashing**.
7. Verify the complete programming output, protected configuration/flash
   recovery expectations, Board A startup and actual application behavior.
   Never infer a hardware PASS from a successful compile or ELF link alone.

The app/config boundary for this project's Board A build is
`0x08013800`; `flash_preflight` and `flash_hex` reject staged images
that extend into `[0x08013800, 0x08020000)`. The protected-range check
examines the image's highest addressed data byte. This is a **data-range
check**, not proof that the MCU's physical erase granularity leaves saved
configuration intact. Always preserve and check recovery/config backups.

Upload sessions are transient to one MCP worker instance; restarting the
worker invalidates partial uploads and staged artifact IDs. At most four
uploads can be open; use `abort_firmware_upload` to reclaim unused slots.

## Alternative: developer-host GDB

The MCP `start_gdb_server` starts **OpenOCD on the Pi** and listens on
port 3333. To load an ELF that resides on Oberon, run GDB **on Oberon**:

```sh
gdb-multiarch /absolute/path/to/build/Project.elf
```

Then, *after an explicit authorized flashing decision*:

```gdb
target extended-remote edge.local:3333
monitor reset halt
load
compare-sections
monitor reset run
```

`load` programs flash. Do not use this sequence for a read-only diagnostic.
Use the actual OpenOCD host/port returned by the MCP deployment. The
`gdb_command` MCP tool instead talks to **GDB running on the Pi**: all
`file`, `symbol-file`, and `load <path>` paths must exist on the Pi.
A tool error `GDB_FILE_NOT_ON_PI` means that condition was not met.
Similarly, a `monitor flash write_image` file path is resolved by OpenOCD
**on the Pi**, not on Oberon. Do not mix these two workflows.

**Only one OpenOCD process should own the probe.** Stop the managed GDB server
before using standalone MCP `flash_hex`; don't run both at once.

## Agent diagnostic rules

- `UPLOAD_INCOMPLETE`: read `next_offset`; send the remaining bytes.
- `CHUNK_OUT_OF_ORDER`: use the worker's expected `next_offset` in bytes.
- `CHUNK_CONFLICT`: a retransmission changed; re-check source file.
- `UPLOAD_SHA256_MISMATCH`: do not flash; regenerate and inspect transport.
- `HEX_MISSING_EOF` / `HEX_NO_DATA`: inspect the **decoded** payload and
  confirm the input was original HEX bytes, not a path or other content.
- `IMAGE_OVERLAPS_PROTECTED_FLASH`: refuse flash; correct the linker/image.
- `GDB_SERVER_OWNS_PROBE`: stop GDB server or use the intentionally selected
  GDB workflow. Never launch a competing OpenOCD session.
- `probe_target`, `fault_snapshot`, and GDB halt commands can **halt the MCU**
  and silence UART AT replies; check target running state and deliberately
  resume when appropriate. A read-only flash preflight does **not** halt.
- If programming fails, capture OpenOCD logs and fault registers **before**
  clearing target state; don't blindly retry or automatically restore TWR.
- Do not modify other boards or run GitHub Actions. Use local tests
  (`python3 -m pytest -q`), and do not create automatic Actions triggers.
