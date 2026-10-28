# Supported Targets

stm-debug-mcp is not tied to a single MCU family. Boards are identified by the ST-Link probe, and the MCU is discovered via its chip ID (`DEV_ID` from the DBGMCU ID register, read with `st-info`/OpenOCD). The chip ID selects the right OpenOCD target config, work-area size, and UID address automatically.

## What works out of the box

The chip ID maps in `src/stm32_mcp/toolchain.py` and `src/stm32_mcp/board_map.py` currently cover:

| Family | OpenOCD config | Chip IDs |
|---|---|---|
| STM32F0 | `stm32f0x.cfg` | 0x440, 0x442, 0x444, 0x445, 0x448 |
| STM32F1 | `stm32f1x.cfg` | 0x410, 0x412, 0x414, 0x418, 0x420, 0x428, 0x430 |
| STM32F2 | `stm32f2x.cfg` | 0x411 |
| STM32F3 | `stm32f3x.cfg` | 0x422, 0x432, 0x438, 0x439, 0x446 |
| STM32F4 | `stm32f4x.cfg` | 0x413, 0x419, 0x421, 0x423, 0x431, 0x433, 0x434, 0x441, 0x458 |
| STM32F7 | `stm32f7x.cfg` | 0x449, 0x451, 0x452 |
| STM32G0 | `stm32g0x.cfg` | 0x456, 0x460, 0x466, 0x467 |
| STM32G4 | `stm32g4x.cfg` | 0x468, 0x469, 0x479 |
| STM32H7 | `stm32h7x.cfg` | 0x450, 0x480, 0x483 |
| STM32L0 | `stm32l0.cfg` | 0x425, 0x417, 0x447, 0x457 |
| STM32L1 | `stm32l1.cfg` | 0x416, 0x427, 0x429, 0x436, 0x437 |
| STM32L4 / L4+ | `stm32l4x.cfg` | 0x415, 0x435, 0x461, 0x462, 0x464, 0x470, 0x471 |
| STM32L5 | `stm32l5x.cfg` | 0x472 |
| STM32WB | `stm32wbx.cfg` | 0x494, 0x495, 0x496 |
| STM32WL | `stm32wlx.cfg` | 0x497 |
| STM32C0 | `stm32c0x.cfg` | 0x443 |
| STM32U5 | `stm32u5x.cfg` | 0x455, 0x476, 0x481, 0x482 |

Build, flash, memory read/write, live monitoring, debug sessions, and serial work for all of the above. Watchpoints depend on the DWT unit: Cortex-M0/M0+ parts (F0, G0, L0, C0) have few or none.

Feature-specific notes:

- **STM32WB FUS/BLE bringup** (`stm32_fus_*`) currently supports STM32WB1x (chip ID 0x494) only.
- If a chip ID is missing from the maps, tools fail with a clear "unknown chip ID" error: nothing is flashed blindly.

## Adding support for more MCUs

Contributions adding families or filling in missing chip IDs are welcome. The chip ID for any part is in its reference manual (`DEV_ID` in the DBGMCU_IDCODE register) or printed by `st-info --chipid`.

1. **`src/stm32_mcp/toolchain.py`: `_CHIPID_TO_TARGET`**: add `chip_id -> "<openocd target>.cfg"`. The config must exist in your OpenOCD installation (`target/` directory).
2. **`src/stm32_mcp/toolchain.py`: `_CHIPID_TO_WORKAREA`** (optional): small RAM work-area size in bytes for parts where the OpenOCD default misbehaves; otherwise omit.
3. **`src/stm32_mcp/board_map.py`: `_CHIPID_TO_UID_ADDR`**: add the chip ID and the address of the 96-bit unique device ID (also from the reference manual) so `stm32_list_probes` can pin board nicknames to the MCU.
4. **STM32WB wireless coprocessor bringup only: `src/stm32_mcp/fus.py` `_WB_INSTALL_MAP`**: add the chip ID with its FUS/BLE image addresses and stack variant.
5. Test on real hardware if you can: `stm32_list_probes`, `stm32_board_info`, and a flash + verify cycle. Open a PR describing the part number tested.

For tooling gaps (missing OpenOCD config, different install layout), `toolchain.py` is also where executable discovery lives: PATH is checked first, then common install locations, so additions there are usually unnecessary.
