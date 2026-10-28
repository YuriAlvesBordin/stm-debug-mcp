# Getting Started

## What you need

**Software**

- Python 3.10 or newer
- [STM32CubeIDE](https://www.st.com/en/development-tools/stm32cubeide.html) — required for the headless builder
- [OpenOCD](https://openocd.org/) (package-manager install or the CubeIDE-bundled one)
- `st-info` from the [stlink open-source tools](https://github.com/stlink-org/stlink) (optional, improves probe detection)
- ARM GCC toolchain on PATH (`arm-none-eabi-nm`, `arm-none-eabi-gdb`) — needed for symbol resolution and struct expansion
- [STM32CubeProgrammer](https://www.st.com/en/development-tools/stm32cubeprog.html) CLI — needed only for STM32WB FUS/BLE bringup

Most executables are discovered automatically: PATH first, then common install locations (`/opt/homebrew`, `/usr/local`, `/usr`, `~/.local/bin`, `~/st/stm32cubeide_*`, `/opt/st/`).

**Hardware**

- An STM32 board with an ST-Link probe connected over USB (SWD)
- The firmware `.elf` file when you want symbol-based tools (read/write by variable name, breakpoints, watchpoints, live memory)

## Install

```bash
git clone https://github.com/YuriAlvesBordin/stm-debug-mcp.git
cd stm-debug-mcp
pip install .            # add.[capture] if you want screenshot capture steps
```

This installs the `stm32-mcp` server plus two standalone CLI helpers:

- `stm32-fus` — FUS/BLE bringup for STM32WB from a shell
- `stm32-serial` — interactive serial terminal sharing the MCP connection

## Configure your MCP client

Add the server to any MCP client (Claude Desktop, ZCode, Cursor, ...):

```json
{
  "mcpServers": {
    "stm32": {
      "command": "stm32-mcp"
    }
  }
}
```

During development you can point at the repo directly:

```json
{
  "mcpServers": {
    "stm32": {
      "command": "python",
      "args": ["-m", "stm32_mcp"],
      "env": { "PYTHONPATH": "/path/to/stm-debug-mcp/src" }
    }
  }
}
```

## Verify it works

With a board connected, ask your MCP client (or run):

```
stm32_list_probes     → lists boards, MCU names, and ST-Link serials
stm32_board_info      → ST-LINK firmware, voltage, CPU, flash size
```

Then try the typical workflow: `stm32_build_and_flash` → `serial_connect` → `serial_send`.

## Multiple boards

Every tool accepts an optional `probe` argument (board nickname, probe nickname, or ST-Link serial). When exactly one board is connected you can omit it. Name your hardware once with `stm32_set_nickname` — see the multi-board section in [tools.md](tools.md).

## Serial bridge (optional)

The server also listens on `127.0.0.1:8765` so you can share the serial connection with a plain terminal (`nc localhost 8765` or `telnet`) while the MCP client keeps control of flashing and debugging. Type `help` at the bridge prompt.
