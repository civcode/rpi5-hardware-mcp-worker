# AGENTS.md — Architecture and Design Philosophy

## Project purpose

The Raspberry Pi 5 Hardware MCP Worker is an agent-oriented rapid-prototyping, hardware experimentation and debugging interface. Its goal is broad, reliable access to native hardware and debugging interfaces, not a controlled test platform or a replacement for GDB and OpenOCD.

## Design principles

- **Capability over restriction:** maximize the agent's available action space. Prefer general-purpose interfaces (native GDB CLI/MI, OpenOCD monitor commands, raw UART RX/TX and control lines) over narrow wrappers.
- **Information-driven autonomy:** comprehensive MCP tool descriptions are part of the interface. Explain parameters/defaults, examples, host/path context, side effects, adapter and firmware quirks, result semantics, failure modes and recovery options. Put actionable diagnostics where the agent encounters a problem; do not rely on README discovery alone.
- **Transparency over enforcement:** document hardware-specific assumptions, warnings and consequences rather than adding allowlists, mandatory workflows, approval gates or board-specific refusals merely to enforce an experimental methodology. Defaults should be useful and overridable. Treat current restrictions as implementation behavior to review, not design goals.
- **Native interfaces first:** preserve general-purpose GDB CLI and MI commands and OpenOCD monitor access. Avoid duplicating native memory, register, breakpoint, halt/resume and reset commands with restrictive MCP wrappers. Convenience tools are additive, not mandatory replacements.
- **Reliable execution:** manage transport, process lifecycle, deadlines, resource ownership and session identity. Validate protocol syntax, types and data integrity. Distinguish technical prerequisites (e.g. one physical SWD owner) from policy restrictions. Avoid hidden target-state changes, silent reattachment/replay or automatic recovery that changes an experiment. Explain uncertainty and recovery choices.
- **Observability:** preserve raw bytes, command output, asynchronous events and metadata; label observations, timing, ranges, hashes and truncation accurately. Distinguish command acceptance from confirmed hardware state. Large data may be stored as artifacts with bounded chunk retrieval.
- **Keep it simple:** broad access and rapid experiments take precedence over elaborate policy frameworks, hardware profiles and bespoke orchestration. Add abstractions only if they remove real friction or expand capabilities.

The worker provides capabilities and accurate information. The agent decides how to use them within the user's task, including experimental design, state changes, interpretation and recovery. Board-specific workflows and historical experiment guidance are examples, not global restrictions. Document current implementation constraints truthfully even when they differ from the target design.

## Scope and development

Plain HTTP on the trusted development LAN is intentional; connection security, authentication and TLS are outside project scope. This does not change electrical or protocol facts (e.g. shared grounds, RTS wired to reset, or simultaneous clients contending for the probe); explain them to the agent.

Prefer local tests and mock hardware for software validation, then explicit live bench checks when requested. Do not run GitHub Actions unless explicitly requested; any new workflows must be manually dispatched by default. Do not claim live validation based on mocked tests.

For every proposed change ask: does this expand capability, improve the agent's understanding, or improve reliability? If it only narrows the available action space, reconsider the design.
