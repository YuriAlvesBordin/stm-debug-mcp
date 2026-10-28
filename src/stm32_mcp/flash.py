"""Flash tools — OpenOCD wrapper for SWD operations."""

import asyncio
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .board_map import resolve_probe_full
from .toolchain import run_openocd

_executor = ThreadPoolExecutor(max_workers=2)

FLASH_TIMEOUT = 30
INFO_TIMEOUT = 10

def _do_flash(
    elf_path: str,
    reset: bool = True,
    verify: bool = True,
    sn: str = "",
    target_cfg: str = "",
    chipid: int = 0,
) -> str:
    """Synchronous flash via OpenOCD — runs in executor thread."""
    if chipid == -1:
        return "ERROR: Probe connected but no target MCU detected. Check board power and SWD connection."
    if not sn or not target_cfg:
        return "ERROR: Could not resolve probe. Use stm32_list_probes to see connected boards."

    if not os.path.isfile(elf_path):
        return f"ERROR: File not found: {elf_path}"

    prog_parts = [f"program {{{elf_path}}}"]
    if verify:
        prog_parts.append("verify")
    if reset:
        prog_parts.append("reset")
    prog_parts.append("exit")
    prog_cmd = " ".join(prog_parts)

    try:
        output = run_openocd(sn, target_cfg, [prog_cmd], timeout=FLASH_TIMEOUT, chipid=chipid)
    except subprocess.TimeoutExpired:
        return f"ERROR: Flash timed out after {FLASH_TIMEOUT}s."
    except FileNotFoundError as e:
        return f"ERROR: {e}"

    if "no device found" in output.lower() or "unable to find" in output.lower():
        return "ERROR: No ST-Link detected. Check USB connection and board power."

    if "read protection" in output.lower():
        return (
            "ERROR: Chip is read-protected. Mass erase needed to unlock.\n"
            "WARNING: This erases all flash contents."
        )

    parts = []

    stlink_match = re.search(r'serial:\s+([0-9A-Fa-f]+)', output)
    if stlink_match:
        parts.append(f"ST-LINK: {stlink_match.group(1)}")

    if "** Programming Finished **" in output:
        parts.append("Flash: OK")
    elif "Error" in output or "error" in output.lower():
        parts.append("Flash: FAILED")

    if verify:
        if "** Verified OK **" in output:
            parts.append("Verify: OK")
        elif "** Verify Failed **" in output:
            parts.append("Verify: FAILED")

    if reset:
        if "Flash: FAILED" in parts:
            parts.append("Reset: not performed (flash failed)")
        else:
            parts.append("Reset: OK")

    if not parts:
        return output.strip()

    for line in output.splitlines():
        stripped = line.strip()
        if "error" in stripped.lower() and "error:" not in "\n".join(parts).lower():
            if stripped and not stripped.startswith("Open On-Chip"):
                parts.append(f"  {stripped}")

    return "\n".join(parts)

def _do_board_info(sn: str = "", target_cfg: str = "", chipid: int = 0) -> str:
    """Synchronous board info via OpenOCD — runs in executor thread."""
    if chipid == -1:
        return "ERROR: Probe connected but no target MCU detected. Check board power and SWD connection."
    if not sn or not target_cfg:
        return "ERROR: Could not resolve probe. Use stm32_list_probes to see connected boards."

    try:
        output = run_openocd(sn, target_cfg, ["init"], timeout=INFO_TIMEOUT, chipid=chipid)
    except subprocess.TimeoutExpired:
        return f"ERROR: Board info timed out after {INFO_TIMEOUT}s."
    except FileNotFoundError as e:
        return f"ERROR: {e}"

    if "no device found" in output.lower() or "unable to find" in output.lower():
        return "ERROR: No ST-Link detected. Check USB connection and board power."

    info = []

    stlink_ver = re.search(r'STLINK\s+(V\S+)', output)
    if stlink_ver:
        info.append(f"ST-LINK FW: {stlink_ver.group(1)}")

    sn_match = re.search(r'serial:\s+([0-9A-Fa-f]+)', output)
    if sn_match:
        info.append(f"ST-LINK SN: {sn_match.group(1)}")

    voltage = re.search(r'Target voltage:\s+([\d.]+)', output)
    if voltage:
        info.append(f"Voltage: {voltage.group(1)}V")

    cpu = re.search(r'(Cortex-M\d\+?|cortex_m\d\+?)', output, re.IGNORECASE)
    if cpu:
        info.append(f"CPU: {cpu.group(1)}")

    flash_size = re.search(r'flash\s+size\s*=\s*(\d+\s*[kKmM]?[bB]?)', output)
    if flash_size:
        info.append(f"Flash: {flash_size.group(1)}")

    chip_match = re.search(r'chip\s+id.*?(0x[0-9a-fA-F]+)', output, re.IGNORECASE)
    if chip_match:
        info.append(f"Chip ID: {chip_match.group(1)}")

    from .board_map import get_board_nickname_for_probe_sn, get_probe_nickname
    probe_nick = get_probe_nickname(sn)
    board_nick = get_board_nickname_for_probe_sn(sn)
    if board_nick:
        info.append(f'Board: "{board_nick}"')
    if probe_nick:
        info.append(f'Probe: "{probe_nick}"')

    if info:
        return "\n".join(info)

    return output.strip()

async def stm32_flash(
    elf_path: str,
    reset: bool = True,
    verify: bool = True,
    probe: str = "",
) -> str:
    """Flash firmware to STM32 board via ST-Link.

    Writes the specified .elf (or .bin/.hex) file to the connected STM32's
    flash memory using OpenOCD over SWD.

    Args:
        elf_path: Absolute path to the firmware file (.elf, .bin, or .hex).
        reset: If true, hard-reset the board after flashing.
        verify: If true, verify flash contents match the file.
        probe: Board nickname, probe nickname, or ST-Link SN to target a specific board.

    Returns:
        Flash result — ST-LINK info, download status, verify result.
    """
    sn, target_cfg, chipid = resolve_probe_full(probe)
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor, lambda: _do_flash(elf_path, reset, verify, sn, target_cfg, chipid)
        ),
        timeout=FLASH_TIMEOUT + 10,
    )

async def stm32_board_info(probe: str = "") -> str:
    """Read board information via ST-Link.

    Connects to the STM32 via SWD and reads device info: ST-LINK version,
    voltage, CPU type, and flash size. Useful for verifying the ST-Link
    connection before building/flashing.

    Args:
        probe: Board nickname, probe nickname, or ST-Link SN to target a specific board.

    Returns:
        Board info — ST-LINK firmware, serial, voltage, CPU, flash size.
    """
    sn, target_cfg, chipid = resolve_probe_full(probe)
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor, lambda: _do_board_info(sn, target_cfg, chipid)
        ),
        timeout=INFO_TIMEOUT + 10,
    )
