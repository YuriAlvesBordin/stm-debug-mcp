# Tool Reference

All 35 tools are registered by the server. Tools that talk to hardware accept an optional `probe` argument (board nickname, probe nickname, or ST-Link serial number); with a single board connected you can omit it.

## Build & flash

| Tool | Description |
|---|---|
| `stm32_build` | Compile a CubeIDE project headless (auto-detects new/deleted source files). |
| `stm32_flash` | Flash an `.elf`/`.bin`/`.hex` via OpenOCD over ST-Link SWD, with optional verify and reset. |
| `stm32_build_and_flash` | Build + flash in one call — the common path. |
| `stm32_board_info` | ST-LINK firmware, voltage, CPU, flash size. |

`project_path` must contain `.project` and `.cproject`. Never hand-edit `Debug/`, `Release/`, `Drivers/`, or `.cproject` — the headless builder manages them.

## Probe & board management

| Tool | Description |
|---|---|
| `stm32_list_probes` | Enumerate connected boards with MCU names, UIDs, and nicknames. |
| `stm32_set_nickname` | Name a board (by MCU UID) or a probe (by ST-Link SN). |

Board nicknames follow the MCU and survive probe swaps; probe nicknames follow the ST-Link, so label probes physically. The map lives in `~/.stm32-mcp/board_map.json`.

## Symbols & memory

| Tool | Description |
|---|---|
| `symbol_info` | Resolve C expressions (`"g_app.dosing.cfg.pid_kp"`, `"tasks[2].lastRunMs"`) to address/size/type. |
| `stm32_read_memory` / `stm32_write_memory` | One-shot read/write by address or symbol (needs `elf_path`). |

Prefer symbol paths over raw addresses everywhere; use `symbol_info` when unsure instead of grepping headers or running GDB by hand.

## Live memory monitoring

| Tool | Description |
|---|---|
| `live_memory_start` | Open a persistent OpenOCD connection and poll variables via SWD into a JSONL file and ring buffer. |
| `live_memory_read` | Read the most recent samples. |
| `live_memory_write` | Edit a variable through the running session — no stop/restart (CubeIDE Live Expressions, with write access). |
| `live_memory_stop` | Stop polling, return session stats. |

Passing a struct symbol auto-expands all fields into dotted names using DWARF info (`blink.state`, `blink.mode`); use `{"symbol": ..., "expand": false}` for raw bytes. Only one live session per probe; stop it before flashing. Sessions auto-reconnect on transient ST-Link drops.

## Real-time debug sessions

| Tool | Description |
|---|---|
| `debug_session_start` / `debug_session_stop` | Persistent OpenOCD session with full CPU control. |
| `debug_status`, `debug_halt`, `debug_resume`, `debug_reset`, `debug_step`, `debug_wait_halt` | Core control. |
| `debug_breakpoint` | Hardware breakpoints (FPB) by symbol or address. |
| `debug_watchpoint` | DWT data watchpoints (r/w/rw, optional value match). |
| `debug_registers` | Core registers (PC, LR, SP, xPSR, or full dump). |
| `debug_mem` | Read/write RAM through the session — works while the core is running. |
| `debug_backtrace` | Heuristic call stack from PC/LR plus a stack scan. |

Typical flow: `debug_session_start(probe, elf_path)` → `debug_breakpoint(... "set", location="fault_handler")` → `debug_reset` → `debug_wait_halt` → inspect (`debug_registers`, `debug_mem`, `debug_backtrace`) → `debug_session_stop` (resumes the core). Only one debug session **or** one live-memory session per probe, never both.

## Serial

| Tool | Description |
|---|---|
| `serial_list_ports` | List serial ports, marking ST-Link VCPs. |
| `serial_connect` / `serial_disconnect` | Open/close a port (default 115200 baud). |
| `serial_send` / `serial_read` | Send with optional response read; read buffered input. |
| `serial_sequence` | Multi-step send/delay/capture/mem-read/mem-write sequence in one call with real timing — for timing-sensitive hardware tests. |

`serial_sequence` steps: `{"send": ..., "to": port}`, `{"delay_ms": ...}`, `{"capture": true, "label": ...}` (saves a PNG; requires the `capture` extra), `{"mem_write": ...}`, `{"mem_read": ...}`. Memory steps accept `symbol` + `elf_path` instead of `address`, and `probe` accepts any nickname form. `on_failure` is `continue` (default) or `stop`; `filter_responses` matches `expect` only against `>`-prefixed VCP lines. Note: each memory step launches a short-lived OpenOCD (tens of ms overhead), so very tight memory-to-memory timing is approximate; delays themselves are accurate.

## STM32WB FUS / BLE bringup

| Tool | Description |
|---|---|
| `stm32_fus_upgrade` | Upgrade FUS on a bare STM32WB (step 1). |
| `stm32_ble_stack_install` | Install the BLE coprocessor stack (step 2). |
| `stm32_fus_bootstrap` | Both steps in one call. |

Only STM32WB1x (chipid 0x494) is wired today — see [supported targets](supported-targets.md). Wireless binaries are resolved from the `wireless_dir` argument, `$STM32_WIRELESS_BINARIES`, or `~/stm32cube-wb/Projects/STM32WB_Copro_Wireless_Binaries`. FUS operations wipe flash sectors 0–3: always re-flash user firmware afterward. The same operations are available from a shell via `stm32-fus {upgrade,stack,bootstrap}`.

## Serial bridge

A TCP bridge on `127.0.0.1:8765` lets a standalone terminal share the MCP serial connection (`nc localhost 8765`): `ports`, `connect <port|nickname>`, `status`, `quit`; anything else is forwarded to the firmware.
