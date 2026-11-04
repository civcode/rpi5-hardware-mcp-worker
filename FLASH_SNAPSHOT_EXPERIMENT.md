# Stock V1.0.1 flash-layout experiments

This worker provides **read-only main-flash snapshots** and **host-only diffs** so
a separate STM32F103 running stock `BU03_BU04_AT_V1.0.1_T2026-09-02.hex` can
be investigated without changing the working ATfE target.

The experiment separates three actions:

1. **Snapshot**: OpenOCD reads main flash over SWD; no erase, programming,
   reset, option-byte access, OTP access, or MCU flash write is requested.
   The CPU is halted briefly and **explicitly resumed**. The target may
   experience a timing interruption. If the tool returns `resumed: false`,
   stop and inspect the target state before continuing.
2. **Change/save**: Only the operator, after separate authorization, sends
   documented stock AT configuration setters and `AT+SAVE`. `AT+SAVE`
   **writes main flash and resets the STM32**; the snapshot tool does not.
3. **Diff**: Compares immutable snapshot binary files on `edge`, without
   touching the target. Produces per-page counts, contiguous runs, a
   per-changed-byte CSV and full JSON.

## Deployment

Use branch `feat/flash-snapshot-diff` on the Raspberry Pi and restart the
existing worker service once after checkout. No Actions workflows are used.
The process must have `openocd` installed and access to the CMSIS-DAP
probe, just as the existing worker does.

Files are saved under `~/rpi5-flash-snapshots` by default, **across worker
restarts**. Optionally set `RPI5_FLASH_SNAPSHOT_DIR=/absolute/path`.

**Connect only the intended stock-firmware target to the worker's probe.**
The worker cannot distinguish two simultaneous boards connected through
similar CMSIS-DAP adapters. Verify the board and firmware version via
`AT+GETVER` before taking measurements.

Only 64- and 128-KiB STM32F103 targets are accepted; flash size is read
from factory information memory in the **same OpenOCD session** as the dump.
A 64-KiB device does not include the historical configuration address
`0x08013800`; flag that mismatch rather than assuming page 78 exists.

Never run a snapshot while a worker-managed GDB server owns the probe.
Likewise, stop any **independent** OpenOCD/GDB sessions first.

## MCP tools

| Tool | Touches MCU? | Behavior |
| --- | --- | --- |
| `flash_snapshot(speed_khz=1000, label="", quirk=False)` | **Yes, temporarily halt/resume** | Detect 64/128-KiB capacity, read all main flash, SHA-256, persist `.bin` and `.json` |
| `flash_snapshot_list()` | No | List up to 100 recent snapshots, full count |
| `flash_snapshot_info(snapshot_id)` | No | Verify persisted image SHA-256 and metadata |
| `flash_snapshot_chunk(snapshot_id, offset=0, length=4096)` | No | Fetch 1–8192 image bytes as Base64, with per-chunk hash |
| `flash_snapshot_diff(before_id, after_id, label="")` | No | Compare images of equal capacity, write full `diff-*.json` and `diff-*.csv`; return summary and first 64 ranges |
| `flash_snapshot_diff_chunk(diff_id, format="csv", offset=0, length=4096)` | No | Fetch 1–8192 bytes of a saved CSV/JSON diff report as Base64 with hashes |

The tool produces a 128-KiB image (if the target reports 128 KiB), or a
64-KiB image (if it reports 64 KiB). Capture files never include STM32
option bytes or DW3000 OTP, and the SDK's presumed configuration sector 78
is **not** hard-coded as the only region checked.

## Snapshot and diff identifiers

All ID-based APIs require the **exact, returned** 32-character lowercase hex
identifier, not a label, path, SHA-256 or example placeholder.

- First call `flash_snapshot_list()` to see whether a valid capture exists.
- If the list is empty, call `flash_snapshot(...)` and inspect `ok`,
  `snapshot_id`, `flash_kib`, `sha256`, and `resumed`.
- Pass that returned `snapshot_id` to `flash_snapshot_info`,
  `flash_snapshot_chunk`, or `flash_snapshot_diff`.
- A successful `flash_snapshot_diff` returns a **different** `diff_id`.
  Use that `diff_id` only for `flash_snapshot_diff_chunk`.
- Never substitute the display labels `stock-baseline` or
  `<baseline ID>` as tool arguments.
- On expected validation errors, the MCP tool returns `ok: false` with an
  `error` field. Do not perform configuration changes until valid baseline
  and control snapshots have been verified.

## AT command length and pacing (important)

The original 2024 firmware's USART1 command buffer holds 64 bytes but wraps
when byte 64 arrives. The MCP worker therefore enforces a conservative limit
of **61 printable ASCII command bytes**, excluding the appended **CRLF**.
The transmitted frame can be **at most 63 bytes including CRLF**.

Both `serial_at_query` and `serial_at_command` enforce this limit before
opening the serial device. Both send **one byte at a time**, with a 5 ms
inter-byte delay and a transmit flush for each byte, matching the successful
workaround in `ai-thinker-bu03-bu04-uwb-kit`. The 5 ms gap is conservative,
not a measured minimum of the 2026 stock firmware.

For this configuration mapping experiment:

- Use only these guarded AT tools for transmitted AT commands.
- Do **not** use `serial_exchange` for AT commands: it is a general raw
  transmit endpoint and bypasses the AT-specific frame-length/pacing guards.
- Never split an AT command across several UART tool calls to bypass length
  checks.
- Do not send a second command until the first response has ended or timed
  out; ensure the module has booted after any approved `AT+SAVE` reset.
- If a desired setter exceeds 61 command bytes, **do not send it**.
  Report its full length and propose a shorter documented numeric
  representation while retaining the same numerical values.
- A command length that is safe under the 2024 source is not proof of an
  identical RX limit in the 2026 binary; keep pacing and monitor responses.

The snapshot and diff tools use SWD and saved host files, not the UART
receiver, so their payload sizes are unrelated to the 64-byte AT buffer.

## Suggested experiment sequence

**Preparation (no device writes)**

1. Back up / identify the target, confirm it runs stock V1.0.1, record
   hardware identity, flash size, firmware hash and initial read-only AT
   getters. Keep the ATfE board disconnected from this probe.
2. `flash_snapshot(label="stock-baseline")`. Save returned `snapshot_id`,
   SHA-256 and path. Confirm `resumed: true` and that `AT` still replies.
3. Take a second `flash_snapshot(label="stock-control-repeat")` **without
   sending any AT write/save**, and diff the two IDs. Ideally no main-flash
   changes. If bytes change, investigate background persistence first.

**Only with explicit authorization for flash-changing AT operations**

4. Issue `AT+SAVE` **without changing a setting**, wait for reboot/AT,
   snapshot `stock-no-change-save`, diff with control. This reveals
   incidental persistent metadata/checksum/counter writes.
5. Send `AT+FILTER=0`, then `AT+SAVE` (stock firmware writes flash and
   resets); verify `AT+PDOAGETCFG` shows Filter 0 and capture
   `stock-filter0`.
6. Send `AT+FILTER=1`, then `AT+SAVE`; read back Filter 1 and capture
   `stock-filter1`. A reversible two-byte or one-byte field is strong
   evidence if it appears at the same offset in both comparisons.
7. Later repeat with `AT+USER_CMD` and safe numerical parameters, using
   one parameter per save, with readback and an explicit change-back step.

Do **not** start with `AT+SETCFG` since this can start the radio role.
Avoid `RESTORE`, production tests and any OTP/calibration-related
operation. Do not run automated repeated `AT+SAVE` loops; every save may
erase a flash page and consumes endurance.

For each step, record **before ID, AT command, expected value, readback,
after ID, diff ID, changed page addresses and candidate offsets**.

Example tool calls:

```text
A = flash_snapshot(speed_khz=1000, label="stock-baseline")
B = flash_snapshot(speed_khz=1000, label="stock-control-repeat")
flash_snapshot_diff(before_id=A.snapshot_id,
                    after_id=B.snapshot_id, label="no-write control")
```

After an authorized `AT+SAVE`, call `flash_snapshot` again and compare.
`flash_snapshot_diff` must never issue AT commands or OpenOCD commands.

## Offline comparison of existing backups

No probe or MCP worker is required for binary backups already available on the host:

```bash
python3 flash_snapshots.py before.bin after.bin --csv changes.csv --json changes.json
```

This accepts complete 64- or 128-KiB raw **main-flash** images; it does not
compare Intel HEX files or program the board. The console prints changed
pages and the first 64 ranges, while `changes.csv` lists every changed byte.

## How to interpret the diff

A typical row of the CSV records a physical flash address, old and new
bytes, plus **candidate** little-endian interpretations at the aligned
4-byte boundary (`uint16_t`, `uint32_t`, IEEE-754 `float`). This is
a discovery aid, not a proof of type or alignment. A packed structure can
hold fields at unaligned offsets; analyze neighboring bytes accordingly.

Compare `0→1` and `1→0` transitions, verify the same address changes
reversibly, and distinguish changes caused by `AT+SAVE` alone. Repeated
copies and checksums can produce additional changed ranges.

When comparing stock V1.0.1 with ATfE, **do not** assume the files have the
same firmware image; diff the snapshots **within the stock-firmware
sequence**, not across different firmware builds. Main-flash code bytes
would otherwise overwhelm the config differences.

## Safety and current verification status

- No flasher, debugger server, serial setter, or reset call is invoked by
  the snapshot/diff tool. Snapshot uses OpenOCD `init; halt; read_memory;
  dump_image; resume; shutdown`; the `resume` is attempted even when
  reading fails, using a Tcl `catch` guard.
- If OpenOCD times out or exits unexpectedly, `resumed` cannot be
  confirmed. Do not assume firmware continued running.
- SWD attach may modify debugging-related MCU registers through installed
  OpenOCD scripts; this is **not** a zero-interaction observation.
- Offline unit tests mock OpenOCD and never access the hardware. **Live
  snapshot behavior on the new target has not yet been verified.**
- The first live operation should be one baseline snapshot and a post-read
  `AT` responsiveness check, with no settings changed.
