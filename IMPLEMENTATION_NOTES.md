# Hardware worker improvement implementation

This branch implements the information-driven architecture in [AGENTS.md](AGENTS.md). No specialized halt/register/memory wrappers were added.

| Improvement | Implementation |
| --- | --- |
| Agent tool descriptions and diagnostics | MCP server instructions and tool docstrings; adapter hints and structured zero-RX diagnostic |
| DTR/RTS and long captures | Deferred explicit line assignment; 60 s / 1 MiB raw RX |
| Raw UART | No global TX gate or confirm token; explicit device paths, optional pacing, empty RX-only exchange |
| GDB native transport | Preserve CLI/MI, path hints on failures, optional asynchronous stop wait and persistent transcript, protect token results from preview overflow |
| Evidence artifacts | Generic info/chunk/list/delete; persistent bytes and SHA-256 |
| Operation restrictions | Remove reset/flash confirmation token requirement; retain expected staged digest, physical capacity and probe ownership; configurable protected-region override |
| Deployment identity | Startup and current source identity, capabilities |
| Evidence metadata | RX SHA-256, adapter, timing, termination; named snapshot and fingerprint ranges |

## Local validation

```bash
python3 -m py_compile *.py tests/*.py
python3 -m unittest discover -s tests -v
```

Unit tests use simulated UART/GDB and mocked OpenOCD; they do not prove hardware behavior. No Actions execution is needed.

## Suggested live acceptance (when running on the Pi)

1. Compare `diagnose_host().loaded_worker` to the intended Git commit and tool schemas.
2. During actual target ranging, compare otherwise identical `serial_capture` calls with DTR false and true at the Debug Probe. Verify valid received data rather than treating the diagnostic as proof of the physical path.
3. Use a continuous 20 s capture (262144-byte cap or larger) and retrieve the artifact by chunk. Verify its SHA-256 and decode raw frames independently. Requested DTR/RTS are not physical line measurements.
4. Issue native MI continue and interrupt, optionally wait for a new stop event, and inspect the transcript. Compare MI state and OpenOCD observations without mixing cached register views. Exercise process loss/recovery without automatic replay.
5. Use existing saved snapshots to compare full/application/config digests. A live snapshot halts/resumes and may perturb timing; decide when appropriate.

The changes have not been live-verified on the Raspberry Pi by this implementation session.
