"""stm32-mcp server — entry point and tool registration."""

import logging

from mcp.server.fastmcp import FastMCP

from .board_map import stm32_list_probes, stm32_set_nickname
from .serial_bridge import start_bridge
from .build import stm32_build, stm32_build_and_flash
from .debug_tools import stm32_read_memory, stm32_write_memory
from .flash import stm32_board_info, stm32_flash
from .fus import stm32_ble_stack_install, stm32_fus_bootstrap, stm32_fus_upgrade
from .serial_tools import (
    serial_connect,
    serial_disconnect,
    serial_list_ports,
    serial_read,
    serial_send,
)
from .serial_sequence import serial_sequence
from .live_memory import live_memory_start, live_memory_stop, live_memory_read, live_memory_write
from .symexpr import symbol_info
from .realtime_debug import (
    debug_session_start,
    debug_session_stop,
    debug_status,
    debug_halt,
    debug_resume,
    debug_step,
    debug_reset,
    debug_wait_halt,
    debug_breakpoint,
    debug_watchpoint,
    debug_registers,
    debug_mem,
    debug_backtrace,
)

INSTRUCTIONS = """\
stm32-mcp: Build, flash, and communicate with STM32 hardware.

## Available Tools

- stm32_build          — Compile firmware using CubeIDE headless builder
- stm32_flash          — Flash .elf/.bin/.hex to board via ST-Link SWD
- stm32_build_and_flash — Build + flash in one step (use this most of the time)
- stm32_board_info     — Read ST-Link and MCU info (device ID, flash size, voltage)
- stm32_list_probes    — Show all connected boards with nicknames and MCU IDs
- stm32_set_nickname   — Name a board (by MCU UID) or probe (by ST-Link SN)
- symbol_info          — Resolve C expressions ("g_app.dosing.cfg.pid_kp") to
                         address/size/type in one call — no grep/GDB needed
- serial_list_ports    — List serial ports (marks ST-Link VCP ports)
- serial_connect       — Open a serial connection
- serial_send          — Send data and read response
- serial_read          — Read buffered serial data
- serial_disconnect    — Close a serial connection
- serial_sequence     — Run multi-step serial+SWD-memory sequences in one call (timing-sensitive tests)
- stm32_read_memory    — Read memory by address or variable name (from ELF symbols)
- stm32_write_memory   — Write memory by address or variable name
- live_memory_start    — Start continuous background memory monitoring via SWD
- live_memory_stop     — Stop a live memory session
- live_memory_read     — Read recent entries from a live memory session
- live_memory_write    — Edit a variable through a running live session (no restart needed)
- debug_session_start  — Start a real-time debug session (persistent OpenOCD)
- debug_session_stop   — Stop a debug session (resumes core by default)
- debug_status         — Core state, location, breakpoints, watchpoints
- debug_halt           — Halt the CPU and report where it stopped
- debug_resume         — Resume execution
- debug_step           — Single-step N instructions
- debug_reset          — Reset target (run or halt)
- debug_wait_halt      — Block until breakpoint hit / halt, then report location
- debug_breakpoint     — Set/clear/list hardware breakpoints (by symbol or address)
- debug_watchpoint     — Set/clear/list DWT data watchpoints (r/w/rw, optional value match)
- debug_registers      — Read core registers (PC, LR, SP, xPSR, r0-r12...)
- debug_mem            — Read/write RAM through the session (works while running)
- debug_backtrace      — Approximate call stack from PC/LR + stack scan
- stm32_fus_upgrade    — Upgrade FUS on a bare STM32WB board (step 1 of bringup)
- stm32_ble_stack_install — Install BLE stack on STM32WB (step 2 of bringup)
- stm32_fus_bootstrap  — One-shot: FUS upgrade + BLE stack full install

## Typical Workflow

1. Edit source files (Core/Src/*.c, Core/Inc/*.h)
2. stm32_build_and_flash(project_path="/path/to/project") — build + flash
3. serial_connect(port="/dev/ttyACM0 (Linux) or /dev/cu.usbmodemXXXX (macOS)") — open VCP
4. serial_send(connection_id="...", data="PING") — test firmware
5. serial_disconnect(connection_id="...") — clean up

## Rules

- **Empty probe auto-selects**: when exactly one board is connected, omit the
  probe argument everywhere (flash, debug, live memory). Pass a probe/board
  nickname or SN only when several boards are attached.
- **Use symbol paths, not raw addresses**: symbol parameters everywhere
  (live_memory variables, debug_mem, stm32_read/write_memory, breakpoints,
  watchpoints) accept plain names ("blink") AND C paths with dots and array
  indices ("g_app.dosing.cfg.pid_kp", "tasks[2].lastRunMs"). Never hand-run
  GDB or grep headers to compute addresses — use symbol_info when unsure.
- **ARM toolchain** (arm-none-eabi-nm, arm-none-eabi-gdb) must be on PATH.
  These usually come from the CubeIDE-bundled toolchain directory. If symbol
  resolution or struct expansion fails with "not found", the toolchain is not
  on PATH — tell the user to install an ARM GCC toolchain (or add the
  CubeIDE-bundled tools/bin directory) to their PATH and restart the server.
- project_path must point to a directory containing .project and .cproject files
- Never edit files in Debug/, Release/, Drivers/, or .cproject
- New .c/.h files are automatically detected by the headless builder
- Always build before flashing
- Always verify behavior over serial after flashing
- Serial default: 115200 baud, LF line endings

## Multi-Board Setup

- stm32_list_probes    — Show all connected boards with nicknames and MCU IDs
- stm32_set_nickname   — Name a board (by MCU UID) or probe (by ST-Link SN)
- Use the probe parameter on stm32_flash, stm32_build_and_flash, and
  stm32_board_info to target by board nickname, probe nickname, or ST-Link SN
- Board nicknames follow the MCU — probe swaps don't affect them
- Probe nicknames follow the ST-Link — label your probes physically

## Debug Tools

- stm32_read_memory   — Read memory by address or variable name (from ELF symbols)
- stm32_write_memory  — Write memory by address or variable name
- Use symbol param with elf_path to read/write by name instead of hex address
- Width auto-detected from ELF symbol size when using symbol names

## Hardware Sequences (serial_sequence)

- serial_sequence runs multiple send/delay/capture/mem_write/mem_read steps in one tool call with real timing
- Serial step:     {"send": "CMD", "to": "/dev/ttyACM0 (Linux) or /dev/cu.usbmodemXXXX (macOS)"}
- Delay step:      {"delay_ms": 500}
- Capture step:    {"capture": true, "label": "name"}   # saves PNG to /tmp/stm32-captures/
- Mem write step:  {"mem_write": true, "address": "0x48000418", "value": "0x40", "probe": "yellow"}
- Mem read step:   {"mem_read": true, "address": "0x48000400", "count": 2, "probe": "yellow", "label": "pre"}
- Mem steps accept "symbol" + "elf_path" instead of "address" to read/write by name
- "probe" accepts board nickname, probe nickname, or ST-Link SN
- "width" is 8/16/32, defaults to 32 (auto-detected from symbol size)
- Optional on send: "expect" (substring match), "read_timeout", "line_ending"
- on_failure: "continue" (default) or "stop"
- filter_responses: true to match expect only against >-prefixed VCP lines
- Timing note: each mem op still launches OpenOCD (~tens of ms overhead), so
  very tight memory-to-memory timing is approximate. Delays themselves are accurate.

## Live Memory Monitoring (Live Expressions)

- live_memory_start opens a persistent OpenOCD connection and polls variables via SWD
- live_memory_read returns recent values from an in-memory ring buffer
- live_memory_write EDITS a variable through the running session — the next
  polls show the new value, no stop/restart needed (this is the equivalent of
  CubeIDE Live Expressions, with write access)
- live_memory_stop kills OpenOCD and returns session stats
- Only one session per probe — stop before flashing or using stm32_read/write_memory
- Output is JSONL at the specified path (default /tmp/live_memory_<id>.jsonl)
- **Struct auto-expansion**: Pass a struct variable name (e.g. `"blink"`) and all
  fields are automatically expanded with dotted names (e.g. `blink.state`,
  `blink.prev_output.changed`). Nested structs are recursively expanded.
  Uses GDB DWARF info — works with `-fshort-enums`, padding, etc.
  To monitor a struct as raw bytes instead, use `{"symbol": "blink", "expand": false}`.
- Sessions auto-reconnect if ST-Link connection drops (up to 3 retries with backoff)

## Real-Time Debug Sessions

- debug_session_start opens a persistent OpenOCD connection with full CPU control:
  halt/resume/step/reset, hardware breakpoints, watchpoints, registers, backtrace
- Typical debugging flow:
  1. debug_session_start(probe="...", elf_path=".../firmware.elf")
  2. debug_breakpoint(session_id, action="set", location="main_loop")
  3. debug_reset(session_id) or debug_resume(session_id)
  4. debug_wait_halt(session_id, timeout_ms=5000) — blocks until breakpoint hit
  5. debug_registers / debug_mem / debug_backtrace to inspect state
  6. debug_session_stop(session_id) — resumes the core, kills OpenOCD
- Breakpoint locations accept ELF symbol names (functions) or hex addresses
- Watchpoints use the DWT unit (few comparators; Cortex-M0/M0+ may have none)
- debug_mem reads RAM while the core is RUNNING (real-time) or halted —
  it reuses the session's OpenOCD instead of launching a second instance
- Only one debug session OR one live_memory session per probe, never both
- Stop the session before flashing or using stm32_read/write_memory (TCL port conflict)
- Backtrace is a heuristic (PC, LR, then thumb return addresses found in the
  stack window) — verify against the map file when it matters

## STM32WB FUS / BLE Stack (first-time bringup)

- stm32_fus_bootstrap(probe="...") runs FUS upgrade then BLE stack full install
- stm32_fus_upgrade and stm32_ble_stack_install are the individual steps
- Only STM32WB1x (chipid 0x494) is wired — other WB chipids error out
- Wireless binaries folder is resolved from wireless_dir arg, then
  $STM32_WIRELESS_BINARIES, then ~/stm32cube-wb/Projects/STM32WB_Copro_Wireless_Binaries
- FUS ops wipe flash sectors [0..3], so always re-flash user firmware with
  stm32_build_and_flash afterward
- Same functions are reachable from a shell via `stm32-fus {upgrade,stack,bootstrap}`
"""

mcp = FastMCP("stm32-mcp", instructions=INSTRUCTIONS)

mcp.tool()(stm32_build)
mcp.tool()(stm32_build_and_flash)
mcp.tool()(stm32_flash)
mcp.tool()(stm32_board_info)
mcp.tool()(stm32_list_probes)
mcp.tool()(stm32_set_nickname)
mcp.tool()(symbol_info)
mcp.tool()(serial_list_ports)
mcp.tool()(serial_connect)
mcp.tool()(serial_send)
mcp.tool()(serial_read)
mcp.tool()(serial_disconnect)
mcp.tool()(serial_sequence)
mcp.tool()(stm32_read_memory)
mcp.tool()(stm32_write_memory)
mcp.tool()(live_memory_start)
mcp.tool()(live_memory_stop)
mcp.tool()(live_memory_read)
mcp.tool()(live_memory_write)
mcp.tool()(debug_session_start)
mcp.tool()(debug_session_stop)
mcp.tool()(debug_status)
mcp.tool()(debug_halt)
mcp.tool()(debug_resume)
mcp.tool()(debug_step)
mcp.tool()(debug_reset)
mcp.tool()(debug_wait_halt)
mcp.tool()(debug_breakpoint)
mcp.tool()(debug_watchpoint)
mcp.tool()(debug_registers)
mcp.tool()(debug_mem)
mcp.tool()(debug_backtrace)
mcp.tool()(stm32_fus_upgrade)
mcp.tool()(stm32_ble_stack_install)
mcp.tool()(stm32_fus_bootstrap)

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    start_bridge()
    mcp.run()

if __name__ == "__main__":
    main()
