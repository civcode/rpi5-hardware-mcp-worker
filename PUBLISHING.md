# Publishing and deployment

Canonical repository: [civcode/rpi5-hardware-mcp-worker](https://github.com/civcode/rpi5-hardware-mcp-worker), separate from the STM32 SDK. The historical OpenOCD worker checkout may still be named `rpi5-openocd-mcp-worker`.

For a fresh checkout on the Raspberry Pi:

```bash
git clone https://github.com/civcode/rpi5-hardware-mcp-worker.git
cd rpi5-hardware-mcp-worker
```

Select the intended branch (feature branches may contain functionality not yet on `main`), follow the README installation instructions, and record the exact deployed revision with `git rev-parse HEAD`. For an update, inspect local changes, stop the existing worker, update the selected branch, run local Python tests, then restart with `./start.sh`. Preserve uncommitted bench work.

Run `python3 -m unittest discover -s tests -v` locally. Do not run GitHub Actions unless explicitly requested; any new workflows are manual-only by default.

The STM32 `ATfE-port` project can link to this worker; keep application firmware and worker code independently buildable and deployable. See [AGENTS.md](AGENTS.md) for the design principles.
