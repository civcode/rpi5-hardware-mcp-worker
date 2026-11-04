# Raspberry Pi 5 hardware MCP worker

A **plain-LAN-HTTP** Model Context Protocol worker for Raspberry Pi 5 hardware access: OpenOCD/SWD through the Raspberry Pi Debug Probe and USB-UART/AT commands (default `/dev/ttyUSB0`) for the STM32F103 BU03/BU04 UWB kit.

This is **not** a replacement for GDB or OpenOCD. The worker runs OpenOCD **on the Pi**, offers a few Pi coding agent tools, and starts/stops an OpenOCD **GDB TCP server on port 3333**. GDB on the Oberon workstation can connect directly to `edge.local:3333` and use the **local ATfE ELF with symbols**. It does not require SSH, TLS, a service manager, or a checkout of the SDK on the Pi. UART uses `pyserial` and shares the existing MCP endpoint.

## Architecture

```text
Oberon (Linux)
  Pi coding agent ------ HTTP :8766/mcp ----> edge.local hardware MCP worker
  gdb-multiarch -------- TCP  :3333 -------> OpenOCD GDB server
  ATfE Build/Project.elf (stays on Oberon)       |
                                                 USB CMSIS-DAP Debug Probe
                                                 SWD --> STM32F103 BU03/BU04
```

MCP controls SWD and UART; native remote GDB is the interactive debugging plane. **One OpenOCD process owns the physical probe at a time.** If the GDB server is active, standalone `probe_target`, `fault_snapshot` and `flash_hex` refuse to start another connection; use `monitor` in GDB or stop the GDB server first.

## Flash snapshots and stock V1.0.1 configuration mapping

The `flash_snapshot` MCP tool temporarily halts the STM32, reads its **entire
detected 64- or 128-KiB main flash** through OpenOCD, explicitly attempts to
resume it, and stores a SHA-256-verified binary under
`~/rpi5-flash-snapshots` (customize with `RPI5_FLASH_SNAPSHOT_DIR`).
It does **not** reset, erase or program the MCU. Installed OpenOCD target
scripts may nevertheless alter debug-related registers during attach.

`flash_snapshot_list`, `flash_snapshot_info`,
`flash_snapshot_chunk`, `flash_snapshot_diff`, and
`flash_snapshot_diff_chunk` operate **entirely on host-side stored files**.
Diffs return changed pages/ranges and write exhaustive JSON and CSV with
per-byte old/new values and candidate little-endian numeric interpretations.
The chunk APIs retrieve saved binaries/reports over MCP without requiring SSH.

For a controlled one-setting-at-a-time stock firmware experiment, see
**[FLASH_SNAPSHOT_EXPERIMENT.md](FLASH_SNAPSHOT_EXPERIMENT.md)**. Sending
`AT+SAVE` is a separate operation that **does** write flash and reset; it
is never initiated by the snapshot/diff tools.

Existing binary backups can also be compared **without any hardware**:

```bash
python3 flash_snapshots.py before.bin after.bin --csv changes.csv --json changes.json
```

## Install on Raspberry Pi 5 (headless Raspberry Pi OS)

```bash
sudo apt update
sudo apt install -y python3 python3-venv openocd usbutils gdb-multiarch
# Clone or unpack this dedicated repo on the Pi:
cd ~/workspace/rpi5-hardware-mcp-worker  # example checkout location on edge
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./start.sh
```

The console process listens on `0.0.0.0:8766` and exposes `http://edge.local:8766/mcp`. Ctrl+C stops it. No systemd unit is required. Use `RPI5_MCP_HOST`, `RPI5_MCP_PORT` and `OPENOCD` environment variables if needed.

Test without the worker: `openocd -f interface/cmsis-dap.cfg -c 'transport select swd' -f target/stm32f1x.cfg -c 'adapter speed 100' -c 'init' -c 'halt' -c 'shutdown'`.

The worker uses the **USB** `interface/cmsis-dap.cfg`, not the Pi 5 GPIO adapter and not `target/rp2040.cfg`. Check the probe firmware and USB connection before blaming the STM32: `lsusb -d 2e8a:000c`; `lsusb -v -d 2e8a:000c | grep bcdDevice`; `sudo dmesg -w`. If an access error occurs, configure a `udev` rule for the USB Debug Probe; do not run the entire MCP process as root without need.

The official Debug Probe **D** cable uses **orange = SWCLK**, **black = GND**, **yellow = SWDIO**. Its three-wire cable provides **no NRST**, so connect-under-reset is limited unless an additional reset connection or manual reset is available. The target must be properly powered at 3.3V logic levels and share ground with the probe. For a homemade Raspberry Pi Pico probe, verify its firmware and wiring separately.

## Configure Pi coding agent on Oberon

```bash
pi mcp add rpi5-hardware --url http://edge.local:8766/mcp
pi mcp list
```

The Raspberry Pi 5 is named `edge`; use its LAN/mDNS hostname `edge.local`, or its IP address if `edge.local` does not resolve from Oberon. The included `pi-mcp.json` is an example user/project MCP configuration. Existing clients registered as `rpi5-openocd` can retain that name while using the same URL. Keep the existing `armcc5` worker on port **8765** and this one on port **8766**.

## MCP tools

| Tool | Effect |
| --- | --- |
| `diagnose_host` | Read-only Pi host/USB/OpenOCD diagnostics, no target contact |
| `inspect_openocd_host` | Host-only executable hash/version and fixed candidate OpenOCD script contents; no target access |
| `probe_target` | **Halts MCU**, reads target ID and flash capacity; no erase/program |
| `fault_snapshot` | **Halts MCU** and reads fault status; CPU-register values are not yet reliably returned by the current tool (see verified findings below). No reset/program |
| `start_gdb_server` | Start one persistent OpenOCD process for remote GDB on `:3333` |
| `gdb_server_status` | Report server state, PID and recent OpenOCD logs |
| `stop_gdb_server` | Stop persistent OpenOCD process |
| `stage_hex` | Stage an Intel HEX image sent as base64; checks checksums, address range and SHA-256; **does not flash** |
| `staged_hex_info` | Rehash a staged HEX and return its generated OpenOCD path; **does not access the target** |
| `flash_hex` | Destructive: probe capacity, **erase/program/verify/reset** a staged image, only with matching SHA-256 plus `confirm="FLASH"` |
| `serial_list_ports` | List configured USB serial devices and presence |
| `serial_capture` | Bounded UART receive without transmission |
| `serial_at_query` | Bounded read-only AT query, complete response and raw bytes |
| `serial_at_command` | Opt-in AT configuration command, requires `confirm="AT_WRITE"` |
| `serial_exchange` | Opt-in bounded Base64-encoded UART transmit, requires `confirm="SEND"` |

`staged_hex_info(artifact_id)` reports the generated path only while the worker and staged artifact exist, after rechecking the file hash; it accepts no user path. A worker restart may invalidate the ID and delete its temporary files. The path is not authorization to program; any future GDB/OpenOCD programming commands require separate review and explicit approval.

`inspect_openocd_host()` reads only a fixed list of script filenames under `OPENOCD_SCRIPTS`, `/usr/local/share/openocd/scripts` and `/usr/share/openocd/scripts`, and runs the configured OpenOCD executable only with `--version`. It returns candidate contents and hashes, not proof of the active search order, embedded Tcl, build source, or deployed flash-driver behavior. No arbitrary file path or command can be supplied.

The optional `quirk=true` parameter requests OpenOCD CMSIS-DAP single-packet compatibility mode (if supported by installed OpenOCD). `speed_khz` defaults to **100** to debug unreliable links. Official probe firmware v2.3.1 or v2.2.3 may be useful as a comparison when diagnosing Pi 5-specific issues.

**Flash normally via GDB `load` instead of passing a large HEX through MCP**: it works directly with the ELF on Oberon, retains debug symbols, and avoids a file transfer. `stage_hex`/`flash_hex` are optional for agent-controlled program/verify tests and perform capacity/hash checks.

## USB-UART and AT commands

The UART tools use the existing plain-HTTP endpoint `http://edge.local:8766/mcp`. Network listeners remain unchanged (`0.0.0.0:8766`; GDB port 3333 when started). No authentication, TLS, or firewall changes are introduced.

- Install dependencies in the Pi virtual environment: `.venv/bin/python -m pip install -r requirements.txt`; this includes `pyserial`.
- Default serial port: `/dev/ttyUSB0`. Configure additional permitted device paths with `RPI5_SERIAL_PORTS=/dev/ttyUSB0,/dev/serial/by-id/your-adapter`. An empty `port` argument selects the first configured path.
- Default UART: **115200 baud, 8N1**, no software or hardware flow control. Calls use finite timeouts, at most **4096 received bytes** and at most **256 transmitted bytes**.
- `serial_list_ports()` reports presence and USB identifiers. `serial_capture(duration_ms=1000)` listens for unsolicited traffic without transmitting bytes.
- `serial_at_query(command="AT+GETVER")` transmits `AT+GETVER\r\n` and returns `rx_base64`, `rx_hex`, `rx_gb18030`, `status` (`OK`, `ERROR`, or `INCOMPLETE`) and `complete`. Read-only allowlist: `AT`, `AT+GETVER`, `AT+GETCFG`, `AT+GETDEV`, `AT+GETUWBMODE`, `AT+PDOAGETCFG`, `AT+GETDLIST`, `AT+GETKLIST`. The quiet-window check refuses to transmit when unsolicited bytes are already present; use `serial_capture` to diagnose chatty firmware.
- To enable setting changes, start the worker with `RPI5_SERIAL_ENABLE_WRITES=1 ./start.sh`, then call `serial_at_command(command="AT+RESTORE", confirm="AT_WRITE")`. For arbitrary bounded byte exchanges use `serial_exchange(tx_base64="...", confirm="SEND")`. Both methods are off by default. The per-call confirmation is an *operation guard*, not network authentication.
- Raw Base64 and hex outputs preserve the exact received bytes; GB18030 text is display-only and may contain replacement characters. This is important for vendor AT responses containing control bytes.
- After detecting a terminal line, queries continue collecting bytes until 100 ms of UART silence or the overall timeout/byte limit, so a split CR/LF is not normally left behind. The terminal marker and bounded quiet period do not prove that delayed unsolicited output or a premature `OK` belongs to the response; inspect the raw bytes when framing is uncertain.
- Expected serial errors (such as a missing pyserial dependency, invalid parameters or a busy port) return `ok: false`, `error` and `error_type`. An error does not establish whether bytes were transmitted: a write or flush can fail after partial transmission. Check `ok` in the returned tool content; these application errors are not MCP transport errors.
- The port is opened with POSIX exclusive access. Close Minicom and other serial processes first. The worker requests deasserted DTR/RTS before opening, but some USB UART drivers may generate transient line changes during open; whether this affects the target depends on wiring.
- The [ai-thinker-bu03-bu04-uwb-kit](https://github.com/civcode/ai-thinker-bu03-bu04-uwb-kit) CLI remains available for high-level configuration/JSON export. The new tools provide lower-level serial/AT access, not full CLI feature parity.

UART tools have offline unit tests using a fake serial port. A connected BU03/BU04 reporting V1.0.1 was tested over `/dev/ttyUSB0`: one-second passive capture returned no bytes and reported no transmit; a separate `AT` returned `\r\nOK\r\n`. Three further trials of each of `AT+GETVER`, `AT+GETCFG`, `AT+GETDEV`, `AT+PDOAGETCFG`, `AT+GETDLIST` and `AT+GETKLIST` (18 queries) returned byte-identical responses per command, marked complete and untruncated, with terminal `OK\r\n`. Hex, Base64 and GB18030 views agreed with the received bytes; responses included control characters. This does not establish framing under delayed output, error responses, port contention or other firmware versions, and does not authorize target operations.

## Verified hardware and remote-debugging results (2026-11-03)

**Host:** Raspberry Pi 5 `edge` / `edge.local` (Linux ARM64); **workstation:** Oberon; **debug adapter:** Raspberry Pi Debug Probe, USB VID:PID `2e8a:000c`; **SWD target:** STM32F103 BU03/BU04. These are owner-reported live hardware tests, not just mock tests.

| Check | Observed result |
| --- | --- |
| OpenOCD | `0.12.0+dev-snapshot (2026-02-16-16:07)`; `interface/cmsis-dap.cfg` and `target/stm32f1x.cfg` installed |
| USB and SWD | CMSIS-DAPv2, SWD DPIDR `0x1ba01477`, Cortex-M3 `r1p1`; target examination succeeded |
| STM32 device | OpenOCD ID `0x20036410` (reported medium density, revision Y); detected **128 KiB flash at 0x08000000** |
| Probe/MCP | `diagnose_host` passed; `probe_target(speed_khz=1000)` passed; nonfatal “unknown state when halt was requested” and deprecated-command warnings were seen |
| Flashing | **Direct OpenOCD CLI**, not the MCP `flash_hex` method, successfully programmed, verified (`Verified OK`), and reset the original `BU03_BU04_AT_V1.0.1_T2026-09-02.hex` |
| Programming speed | An initial `adapter speed 5000` request reported an actual clock of **1000 kHz**; programming and verification succeeded |
| Original firmware UART | Minicom `AT` → `OK`; `AT+PDOAGETCFG` → `Dlist:1 KList:1 Net:1111 AncID:65535`; `AT+GETKLIST` returned an empty list. The separate configuration CLI exported firmware `V1.0.1` with build date `Sep 2 2026 17:35:08` |
| MCP fault snapshot | `fault_snapshot(speed_khz=1000)` returned **fault status** but reported register names without CPU-register values: a known tool limitation |
| MCP GDB server | `start_gdb_server(speed_khz=1000, port=3333)` succeeded; status indicated listening on 3333 |
| Remote GDB | `gdb-multiarch` on Oberon connected using `target extended-remote edge.local:3333`, read CPU registers and flash instructions, and executed `monitor resume` followed by `detach` |

The GDB session, **without loading a mismatched ELF**, observed `pc=0x0800d25c`, `msp=0x20004e80`, and a vector table starting with `0x20004f30` at `0x08000000`. This confirms live register and memory access; the instruction address is **not** a source location in the historical 2024 AXF. The 2026 HEX has no matching symbol-rich ELF in the project.

**Fault-status interpretation:** In the captured session `CFSR=0` and `HFSR=0`, indicating no set fault-status bits at that moment. The observed MMFAR/BFAR contents varied between captures, and the corresponding CFSR validity bits were clear, so those values **must not be interpreted as valid fault addresses**.

**Known limitations:** The current worker `fault_snapshot` uses OpenOCD's bare `reg` command; with this installed OpenOCD version the observed snapshot did not provide numeric PC/SP/general-register values. Until fixed and verified, use GDB `info registers` or explicit OpenOCD register reads. The 2026 firmware was verified as programmed and AT-responsive; **PDoA ranging and ATfE on-target execution are not yet verified**. The worker's `stage_hex`/`flash_hex` endpoints have not been hardware-tested.

### Reproduce a non-flashing GDB inspection

Start the MCP worker on `edge` with `./start.sh`. From the Pi coding agent on Oberon, call `diagnose_host`, then `probe_target(speed_khz=1000)`, optionally `fault_snapshot(speed_khz=1000)`, and finally `start_gdb_server(speed_khz=1000, port=3333)`. **The target-probe and fault-snapshot calls halt the MCU**; the server owns the SWD probe afterward, so do not run additional standalone OpenOCD sessions until it stops.

On Oberon, while the **original 2026 HEX** is running, use GDB **without an executable file**:

```bash
gdb-multiarch -q
```

```gdb
target extended-remote edge.local:3333
monitor halt
info registers
x/8wx 0x08000000
x/8i $pc
monitor mdw 0xE000ED28 1   # CFSR
monitor mdw 0xE000ED2C 1   # HFSR
monitor mdw 0xE000ED34 1   # MMFAR (only if valid)
monitor mdw 0xE000ED38 1   # BFAR (only if valid)
monitor resume
detach
quit
```

Ask the coding agent to call `stop_gdb_server` once disconnected. Recheck `AT` on the UART to verify normal execution; that final post-debug AT response is a **follow-up test**, not yet recorded as passed.

For **future ATfE source-level debugging**, open the locally built `Project.elf` only when it corresponds to the firmware actually programmed on the target. **Do not load `Projects/USER/Output/Project.axf` as symbols for the running 2026 HEX**: it belongs to the different 2024 build. A GDB `load` command **programs flash** and is not part of this read-only test.

## GDB workflow from Oberon

First, ask Pi coding agent to call `diagnose_host`, then `probe_target`, then `start_gdb_server`. **This target was measured at 128 KiB flash**; always recheck capacity for another board. The following example is for a **future matching ATfE build**, not the currently running original 2026 HEX. On Oberon:

```bash
./tools/build-atfe.sh debug 128

gdb-multiarch build/atfe-debug/Project.elf
```

Inside GDB:

```gdb
target extended-remote edge.local:3333
monitor halt
info registers
monitor mdw 0xE000ED28 1   # CFSR
monitor mdw 0xE000ED2C 1   # HFSR
monitor mdw 0xE000ED34 1   # MMFAR
monitor mdw 0xE000ED38 1   # BFAR
bt
# Only after capturing initial state, perform controlled reset:
monitor reset halt
thbreak main
continue
# Explicitly destructive, only when approved and backup/capacity verified:
# monitor reset halt
# load
# compare-sections
```

OpenOCD supports `monitor` commands over the existing GDB connection. Do **not** run standalone `probe_target` while GDB already owns the probe. If GDB TCP port is not reachable, check the host/IP and Pi firewall/network; the worker passes `bindto 0.0.0.0` to the OpenOCD server, with Tcl and telnet ports disabled.

## Firmware validation order

1. **Completed:** USB CMSIS-DAP/SWD target detection; record 128 KiB flash size and device ID.
2. **Completed:** direct OpenOCD programming/verification of the original 2026 HEX and AT UART baseline.
3. **Completed:** MCP diagnostics/target probe/GDB server; Oberon remote GDB register, memory, disassembly and detach checks.
4. **Outstanding:** fix numeric CPU-register capture in `fault_snapshot` and verify it on the live device.
5. **Next:** debug minimal `firmware_core.elf` and full ATfE `Project.elf` only after explicitly authorized programming and with a matching symbol file; investigate startup/stack/clock/interrupts/ABI.

## Tests and scope

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py worker_core.py serial_core.py
```

Unit tests mock OpenOCD. **Live Pi 5 USB/SWD access, MCP diagnostics/probe/GDB-server control, remote GDB, and direct OpenOCD programming plus verification of the reference HEX are now verified on hardware (2026-11-03).** The MCP flash endpoints and ATfE firmware runtime remain unverified. UART query testing above used the running MCP service; its exact deployed commit was not independently verified. Minicom conflict handling and worker restart remain untested. The service is intentionally unauthenticated and unencrypted for the trusted local LAN, as requested. It can halt the target and optionally write flash; keep port 8766 on that LAN. There are **no GitHub Actions workflows** and nothing starts automatically after reboot.

## Repository role

Canonical GitHub repository: `civcode/rpi5-hardware-mcp-worker` (separate from `civcode/STM32F103-BU0x_SDK`). The STM32 `ATfE-port` project already contains the basic `tools/rpi5-debug` shell scripts and `docs/RPI5_OPENOCD_DEBUG.md`; the hardware MCP worker adds remote SWD and UART orchestration and should link to those instructions rather than duplicating their logic in the firmware repository.

Reference documentation: https://openocd.org/doc/html/Server-Configuration.html , https://openocd.org/doc/html/Debug-Adapter-Configuration.html , https://github.com/raspberrypi/debugprobe/releases , https://py.sdk.modelcontextprotocol.io/run/ .
