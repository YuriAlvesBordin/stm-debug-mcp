# Architecture

One Python package, `src/stm32_mcp/`, exposing a single MCP server plus two standalone CLIs. External tools (CubeIDE headless builder, OpenOCD, `st-info`, `STM32_Programmer_CLI`, `arm-none-eabi-*`) are wrapped, not reimplemented.

## Module map

| Module | Responsibility |
|---|---|
| `server.py` | Entry point: FastMCP instance, tool registration, LLM instructions. Starts the serial bridge and runs the MCP server. |
| `toolchain.py` | Discovery of external executables (PATH first, then common install dirs), OpenOCD invocation helper (`run_openocd`), chip-ID → target config / work-area / UID-address maps. |
| `board_map.py` | Probe enumeration via `st-info`/OpenOCD, MCU chip-ID decoding, persistent nickname map at `~/.stm32-mcp/board_map.json`, probe resolution (nickname → ST-Link SN). |
| `build.py` | CubeIDE headless build (`stm32cubeide --launcher.suppressErrors -data <workspace> -application org.eclipse.cdt.managedbuilder.core.headlessbuild`), with stale-build-process cleanup and timeouts. |
| `flash.py` | One-shot OpenOCD flash/verify/reset and board info; output parsed into a compact status report. |
| `debug_tools.py` | One-shot memory read/write by address or ELF symbol (spawns a short-lived OpenOCD). |
| `realtime_debug.py` | Persistent debug session: long-lived OpenOCD process driven over its TCL TCP port — halt/resume/step/reset, breakpoints (FPB), watchpoints (DWT), registers, real-time memory access, heuristic backtrace. |
| `live_memory.py` | Live-expression monitor: background thread polls variables through a persistent OpenOCD TCL connection, writes JSONL, keeps a ring buffer, auto-reconnects on transient drops, and supports live writes. |
| `struct_layout.py` / `symexpr.py` | DWARF-based struct layout (via `arm-none-eabi-gdb` batch mode) and C-expression symbol resolution (`g_app.dosing.cfg.pid_kp`, array indices) used to expand structs into dotted field paths. |
| `serial_tools.py` / `serial_client.py` | Pyserial connection pool (VCP detection via ST VID/PID) and the `stm32-serial` interactive terminal. |
| `serial_sequence.py` | Multi-step serial + SWD-memory sequencer with deterministic delays and optional screenshot capture (`opencv-python`, optional extra). |
| `serial_bridge.py` | TCP server on `127.0.0.1:8765` sharing the MCP serial connection with external terminals. |
| `fus.py` | STM32WB FUS upgrade and BLE stack install via `STM32_Programmer_CLI -fwupgrade`, plus the `stm32-fus` CLI. |

## Key design decisions

- **Chip-ID-driven configuration.** `st-info`/OpenOCD reads `DEV_ID`; maps in `toolchain.py`/`board_map.py` select the OpenOCD target config, work-area size, and UID address. No per-board manual setup.
- **Persistent vs one-shot OpenOCD.** Simple operations (flash, single reads) spawn a short-lived OpenOCD (~tens of ms overhead). Debug sessions and live memory hold one OpenOCD process and drive it over its TCL port — only one such session per probe at a time (fixed TCL port).
- **Symbols over addresses.** All debug/monitoring tools accept plain names or dotted C paths, resolved from the firmware ELF against DWARF info, so callers never compute addresses by hand.
- **Nicknames as stable identities.** Board nicknames are keyed by MCU UID (survive probe swaps); probe nicknames by ST-Link serial.
- **Sync core, async shell.** Tools are `async` (MCP requirement); blocking subprocess/serial work runs in executor threads.

## Known limitations

- Tested platforms are Linux and macOS; Windows is not supported yet (`readline`, path assumptions).
- `serial_sequence` memory steps launch one OpenOCD per step — inter-step timing below ~50 ms is approximate.
- STM32WB bringup covers STM32WB1x only (see [supported-targets.md](supported-targets.md)).
- The debug backtrace is a heuristic stack scan, not full unwinding — verify against the map file when precision matters.

## Contributing

Issues and PRs are welcome. For new MCU support see [supported-targets.md](supported-targets.md); for code changes, keep tools documented in the same style (docstrings double as MCP tool descriptions) and run `ruff check src/` before submitting.
