# Raspberry Pi 5 OpenOCD MCP worker

A minimal **plain-LAN-HTTP** Model Context Protocol worker for the **Raspberry Pi Debug Probe / Pico with debugprobe firmware** attached over USB to a Raspberry Pi 5, debugging an STM32F103 BU03/BU04 over SWD.

This is **not** a replacement for GDB or OpenOCD. The worker runs OpenOCD **on the Pi**, offers a few Pi coding agent tools, and starts/stops an OpenOCD **GDB TCP server on port 3333**. GDB on the Oberon workstation can connect directly to `rpi5:3333` and use the **local ATfE ELF with symbols**. It does not require SSH, TLS, a service manager, or a checkout of the SDK on the Pi.

## Architecture

```text
Oberon (Linux)
  Pi coding agent ------ HTTP :8766/mcp ----> rpi5 OpenOCD MCP worker
  gdb-multiarch -------- TCP  :3333 -------> OpenOCD GDB server
  ATfE Build/Project.elf (stays on Oberon)       |
                                                 USB CMSIS-DAP Debug Probe
                                                 SWD --> STM32F103 BU03/BU04
```

MCP is the control plane; native remote GDB is the interactive debugging plane. **One OpenOCD process owns the physical probe at a time.** If the GDB server is active, standalone `probe_target`, `fault_snapshot` and `flash_hex` refuse to start another connection; use `monitor` in GDB or stop the GDB server first.

## Install on Raspberry Pi 5 (headless Raspberry Pi OS)

```bash
sudo apt update
sudo apt install -y python3 python3-venv openocd usbutils gdb-multiarch
# Clone or unpack this dedicated repo on the Pi:
cd ~/rpi5-openocd-mcp-worker
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./start.sh
```

The console process listens on `0.0.0.0:8766` and exposes `http://rpi5:8766/mcp`. Ctrl+C stops it. No systemd unit is required. Use `RPI5_MCP_HOST`, `RPI5_MCP_PORT` and `OPENOCD` environment variables if needed.

Test without the worker: `openocd -f interface/cmsis-dap.cfg -c 'transport select swd' -f target/stm32f1x.cfg -c 'adapter speed 100' -c 'init' -c 'halt' -c 'shutdown'`.

The worker uses the **USB** `interface/cmsis-dap.cfg`, not the Pi 5 GPIO adapter and not `target/rp2040.cfg`. Check the probe firmware and USB connection before blaming the STM32: `lsusb -d 2e8a:000c`; `lsusb -v -d 2e8a:000c | grep bcdDevice`; `sudo dmesg -w`. If an access error occurs, configure a `udev` rule for the USB Debug Probe; do not run the entire MCP process as root without need.

The official Debug Probe **D** cable uses **orange = SWCLK**, **black = GND**, **yellow = SWDIO**. Its three-wire cable provides **no NRST**, so connect-under-reset is limited unless an additional reset connection or manual reset is available. The target must be properly powered at 3.3V logic levels and share ground with the probe. For a homemade Raspberry Pi Pico probe, verify its firmware and wiring separately.

## Configure Pi coding agent on Oberon

```bash
pi mcp add rpi5-openocd --url http://rpi5:8766/mcp
pi mcp list
```

Use the Pi 5's IP address instead of `rpi5` if name resolution is not configured. The included `pi-mcp.json` is an example user/project MCP configuration. Keep the existing `armcc5` worker on port **8765** and this one on port **8766**.

## MCP tools

| Tool | Effect |
| --- | --- |
| `diagnose_host` | Read-only Pi host/USB/OpenOCD diagnostics, no target contact |
| `probe_target` | **Halts MCU**, reads target ID and flash capacity; no erase/program |
| `fault_snapshot` | **Halts MCU**, records Cortex-M3 registers and fault status; no reset/program |
| `start_gdb_server` | Start one persistent OpenOCD process for remote GDB on `:3333` |
| `gdb_server_status` | Report server state, PID and recent OpenOCD logs |
| `stop_gdb_server` | Stop persistent OpenOCD process |
| `stage_hex` | Stage an Intel HEX image sent as base64; checks checksums, address range and SHA-256; **does not flash** |
| `flash_hex` | Destructive: probe capacity, **erase/program/verify/reset** a staged image, only with matching SHA-256 plus `confirm="FLASH"` |

The optional `quirk=true` parameter requests OpenOCD CMSIS-DAP single-packet compatibility mode (if supported by installed OpenOCD). `speed_khz` defaults to **100** to debug unreliable links. Official probe firmware v2.3.1 or v2.2.3 may be useful as a comparison when diagnosing Pi 5-specific issues.

**Flash normally via GDB `load` instead of passing a large HEX through MCP**: it works directly with the ELF on Oberon, retains debug symbols, and avoids a file transfer. `stage_hex`/`flash_hex` are optional for agent-controlled program/verify tests and perform capacity/hash checks.

## GDB workflow from Oberon

First, ask Pi coding agent to call `diagnose_host`, then `probe_target`, then `start_gdb_server`. Confirm that physical STM32 flash is **128 KiB**, because the current full ATfE firmware is larger than 64 KiB. On Oberon:

```bash
./tools/build-atfe.sh debug 128

gdb-multiarch build/atfe-debug/Project.elf
```

Inside GDB:

```gdb
target extended-remote rpi5:3333
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

1. Check probe firmware / USB enumeration and reproduce the known failure at 100 kHz.
2. `probe_target` without writing flash; record the STM32 flash size and USB errors.
3. Start remote GDB and debug the known-good vendor firmware on a spare or backed-up board.
4. Debug minimal `firmware_core.elf` to isolate startup, stack, clock, SysTick and interrupts.
5. Debug full `Project.elf`, collecting CFSR/HFSR and PCs before resetting. Compare ST libraries, runtime initialization and vendor archive ABI behavior.

## Tests and scope

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py worker_core.py
```

Unit tests mock OpenOCD and do not connect to physical hardware. **Live Pi 5 probe, OpenOCD MCP transport and STM32 flashing are not yet verified.** The service is intentionally unauthenticated and unencrypted for the trusted local LAN, as requested. It can halt the target and optionally write flash; keep port 8766 on that LAN. There are **no GitHub Actions workflows** and nothing starts automatically after reboot.

## Repository role

Suggested canonical GitHub repository: `civcode/rpi5-openocd-mcp-worker` (separate from `civcode/STM32F103-BU0x_SDK`). The STM32 `ATfE-port` project already contains the basic `tools/rpi5-debug` shell scripts and `docs/RPI5_OPENOCD_DEBUG.md`; the dedicated MCP worker adds remote orchestration and should link to those instructions rather than duplicating their logic in the firmware repository.

Reference documentation: https://openocd.org/doc/html/Server-Configuration.html , https://openocd.org/doc/html/Debug-Adapter-Configuration.html , https://github.com/raspberrypi/debugprobe/releases , https://py.sdk.modelcontextprotocol.io/run/ .
