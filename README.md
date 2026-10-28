# stm-debug-mcp

An MCP (Model Context Protocol) server that lets AI coding agents build, flash, debug, and communicate with STM32 microcontrollers over ST-Link — end to end, in the loop.

Point it at a CubeIDE project and a connected board, and your agent can compile firmware, flash it over SWD, exercise it over the serial VCP, watch variables live, set hardware breakpoints, and triage faults — without leaving the conversation.

## Highlights

- **Build & flash** — headless CubeIDE builds, OpenOCD flashing with verify/reset, one-call build-and-flash
- **Real-time debugging** — persistent OpenOCD sessions with halt/step/reset, hardware breakpoints (FPB), watchpoints (DWT), register access, and heuristic backtraces
- **Live expressions** — continuous SWD monitoring of variables (structs auto-expand via DWARF) with live write-back, like CubeIDE Live Expressions from your editor
- **Symbol-native** — every memory/debug tool accepts C paths (`g_app.dosing.cfg.pid_kp`) resolved from the ELF; no hand-computed addresses
- **Serial** — connection pooling, request/response helpers, and a multi-step sequencer with deterministic timing for hardware-in-the-loop tests
- **Multi-board** — name boards and probes; every tool targets by nickname or serial
- **Broad target coverage** — F0/F1/F2/F3/F4/F7/G0/G4/H7/L0/L1/L4/L5/WB/WL/C0/U5 detected automatically by chip ID

## Quick start

```bash
pip install .
```

Then register it with your MCP client:

```json
{
  "mcpServers": {
    "stm32": { "command": "stm32-mcp" }
  }
}
```

Requirements (STM32CubeIDE, OpenOCD, ARM GCC on PATH) and setup details: [docs/getting-started.md](docs/getting-started.md).

## Documentation

- [Getting started](docs/getting-started.md) — requirements, installation, MCP client configuration
- [Tool reference](docs/tools.md) — all 35 tools, parameters, and typical workflows
- [Supported targets](docs/supported-targets.md) — chip ID coverage and how to add new MCUs
- [Architecture](docs/architecture.md) — module map, design decisions, known limitations

## Scope

Platforms: Linux and macOS. Debugging features depend on ST-Link SWD; serial features on the ST-Link VCP or any USB-UART port.

## License

[MIT](LICENSE) © Yuri Alves Bordin
