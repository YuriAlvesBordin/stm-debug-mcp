"""Debug tools — memory read/write with ELF symbol resolution."""

import asyncio
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

from . import ocd_pool
from .board_map import resolve_probe_full
from .hexfmt import annotate_hex
from .symexpr import resolve_symbol_flexible
from .toolchain import find_nm

_executor = ThreadPoolExecutor(max_workers=2)

MEMORY_TIMEOUT = 10

_LTO_SUFFIX_RE = re.compile(
    r"^(?P<base>[A-Za-z_]\w*)"
    r"(?:\.(?:constprop|isra|lto|part|unified|llvm)\.\d+|\.\d+)*$"
)

def _lto_base_name(name: str) -> str:
    """Strip GCC LTO/optimization suffixes: 'filtered_rate.3' ->
    'filtered_rate', 'Scheduler_AddTask.constprop.0.isra.0' -> 'Scheduler_AddTask'."""
    m = _LTO_SUFFIX_RE.match(name)
    return m.group("base") if m else name

def _resolve_symbol(elf_path: str, symbol_name: str) -> tuple[int, int] | None:
    """Resolve a symbol name to (address, size) using arm-none-eabi-nm.

    Runs: arm-none-eabi-nm -S --defined-only <elf_path>
    Output format: 20000304 00000010 B blink
                   (address) (size)  (type) (name)

    LTO builds rename statics with optimizer suffixes ('name.3',
    'fn.constprop.0.isra.0'); exact match wins, otherwise the symbol whose
    LTO-stripped base equals the requested name is used.

    Returns (address, size) or None if not found.
    """
    nm = find_nm()
    if not nm:
        return None

    try:
        result = subprocess.run(
            [nm, "-S", "--defined-only", elf_path],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    exact = None
    renamed = None
    for line in result.stdout.splitlines():
        parts = line.strip().split()

        hit = None
        if len(parts) >= 4 and parts[3] == symbol_name:
            try:
                hit = (int(parts[0], 16), int(parts[1], 16))
            except ValueError:
                continue
        elif len(parts) >= 3 and parts[2] == symbol_name:
            try:
                hit = (int(parts[0], 16), 4)
            except ValueError:
                continue
        elif renamed is None and len(parts) >= 4 and _lto_base_name(parts[3]) == symbol_name:
            try:
                renamed = (int(parts[0], 16), int(parts[1], 16))
            except ValueError:
                continue
        if hit is not None:
            if exact is None:
                exact = hit
    if exact is not None:
        return exact
    return renamed

def _width_from_size(size: int) -> int:
    """Auto-detect access width from symbol size."""
    if size == 1:
        return 8
    elif size == 2:
        return 16
    else:
        return 32

def _ocd_read_cmd(width: int) -> str:
    """OpenOCD memory display command by width."""
    return {8: "mdb", 16: "mdh", 32: "mdw"}.get(width, "mdw")

def _ocd_write_cmd(width: int) -> str:
    """OpenOCD memory write command by width."""
    return {8: "mwb", 16: "mwh", 32: "mww"}.get(width, "mww")

def parse_write_value(value: str, width: int) -> tuple:
    """Parse a write value: hex/decimal int, or float (IEEE 754, width 32 only).

    Returns (int_value, "") on success or (None, error_message).
    """
    try:
        return int(value, 0), ""
    except ValueError:
        pass
    try:
        f = float(value)
    except ValueError:
        return None, f"Invalid value: {value}"
    if width != 32:
        return None, f"Float values require width 32 (got {width})."
    import struct
    return struct.unpack("<I", struct.pack("<f", f))[0], ""

def _do_read_memory(
    address: str,
    symbol: str,
    elf_path: str,
    count: int,
    width: int,
    sn: str,
    target_cfg: str,
    chipid: int = 0,
) -> str:
    """Synchronous memory read — runs in executor thread."""
    if not sn or not target_cfg:
        return "ERROR: Could not resolve probe. Use stm32_list_probes to see connected boards."

    resolved_addr = 0
    sym_label = ""

    if symbol:
        if not elf_path:
            return "ERROR: elf_path is required when using symbol name."
        if not os.path.isfile(elf_path):
            return f"ERROR: ELF file not found: {elf_path}"

        result = resolve_symbol_flexible(elf_path, symbol)
        if result is None:
            return f'ERROR: Symbol "{symbol}" not found in {os.path.basename(elf_path)}.'

        resolved_addr, size = result
        sym_label = symbol

        if width == 32:
            width = _width_from_size(size)
    elif address:
        try:
            resolved_addr = int(address, 0)
        except ValueError:
            return f"ERROR: Invalid address: {address}"
    else:
        return "ERROR: Provide either address or symbol."

    read_cmd = _ocd_read_cmd(width)
    cmd = f"{read_cmd} 0x{resolved_addr:08X} {count}"

    try:
        conn = ocd_pool.acquire(sn, target_cfg, chipid)
        output = ocd_pool.run_cmds(conn, [cmd], timeout_s=MEMORY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"ERROR: Memory read timed out after {MEMORY_TIMEOUT}s."
    except (FileNotFoundError, RuntimeError) as e:
        return f"ERROR: {e}"

    values = []
    for line in output.splitlines():
        m = re.match(r'0x[0-9a-fA-F]+:\s+(.*)', line.strip())
        if m:
            hex_vals = m.group(1).strip().split()
            for v in hex_vals:
                if re.match(r'^[0-9a-fA-F]+$', v):
                    values.append(f"0x{v}")

    if not values:

        if "error" in output.lower():
            return f"ERROR: Memory read failed.\n{output.strip()}"
        return f"ERROR: Could not parse OpenOCD output.\n{output.strip()}"

    addr_str = f"0x{resolved_addr:08X}"
    if sym_label:
        label = f"{sym_label} ({addr_str})"
    else:
        label = addr_str

    return f"{label}: {annotate_hex(values[:count], width)}"

def _do_write_memory(
    address: str,
    symbol: str,
    elf_path: str,
    value: str,
    width: int,
    sn: str,
    target_cfg: str,
    chipid: int = 0,
) -> str:
    """Synchronous memory write — runs in executor thread."""
    if not sn or not target_cfg:
        return "ERROR: Could not resolve probe. Use stm32_list_probes to see connected boards."

    if not value:
        return "ERROR: value is required."

    try:
        write_val = int(value, 0)
    except ValueError:
        return f"ERROR: Invalid value: {value}"

    resolved_addr = 0
    sym_label = ""

    if symbol:
        if not elf_path:
            return "ERROR: elf_path is required when using symbol name."
        if not os.path.isfile(elf_path):
            return f"ERROR: ELF file not found: {elf_path}"

        result = resolve_symbol_flexible(elf_path, symbol)
        if result is None:
            return f'ERROR: Symbol "{symbol}" not found in {os.path.basename(elf_path)}.'

        resolved_addr, size = result
        sym_label = symbol
        if width == 32:
            width = _width_from_size(size)
    elif address:
        try:
            resolved_addr = int(address, 0)
        except ValueError:
            return f"ERROR: Invalid address: {address}"
    else:
        return "ERROR: Provide either address or symbol."

    write_cmd = _ocd_write_cmd(width)
    cmd = f"{write_cmd} 0x{resolved_addr:08X} 0x{write_val:X}"

    try:
        conn = ocd_pool.acquire(sn, target_cfg, chipid)
        output = ocd_pool.run_cmds(conn, [cmd], timeout_s=MEMORY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"ERROR: Memory write timed out after {MEMORY_TIMEOUT}s."
    except (FileNotFoundError, RuntimeError) as e:
        return f"ERROR: {e}"

    if "error" in output.lower() and "shutdown" not in output.lower():
        for line in output.splitlines():
            if "error" in line.lower():
                return f"ERROR: {line.strip()}"

    addr_str = f"0x{resolved_addr:08X}"
    val_str = f"0x{write_val:02X}" if width == 8 else f"0x{write_val:04X}" if width == 16 else f"0x{write_val:08X}"

    if sym_label:
        return f"Wrote {val_str} to {sym_label} ({addr_str})"
    return f"Wrote {val_str} to {addr_str}"

async def stm32_read_memory(
    address: str = "",
    symbol: str = "",
    elf_path: str = "",
    count: int = 1,
    width: int = 32,
    probe: str = "",
) -> str:
    """Read memory by address or variable name (from ELF symbols).

    Reads memory from the connected STM32 via OpenOCD. Can specify a raw
    hex address or a symbol resolved from the ELF — a plain variable name
    or a C path like "g_app.dosing.cfg.pid_kp".

    Args:
        address: Hex address like "0x20000304". Mutually exclusive with symbol.
        symbol: Variable name ("blink") or C path ("app.state.rate"). Requires elf_path.
        elf_path: Path to .elf file for symbol resolution.
        count: Number of units to read (default 1).
        width: Access width — 8, 16, or 32 bits. Auto-detected from symbol size.
        probe: Board nickname, probe nickname, or ST-Link SN. Empty auto-selects
            the only connected board.

    Returns:
        Memory contents formatted as hex values.
    """
    sn, target_cfg, chipid = resolve_probe_full(probe)
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor,
            lambda: _do_read_memory(address, symbol, elf_path, count, width, sn, target_cfg, chipid),
        ),
        timeout=MEMORY_TIMEOUT + 10,
    )

async def stm32_write_memory(
    address: str = "",
    symbol: str = "",
    elf_path: str = "",
    value: str = "",
    width: int = 32,
    probe: str = "",
) -> str:
    """Write memory by address or variable name.

    Writes a value to memory on the connected STM32 via OpenOCD. Can specify
    a raw hex address or a symbol resolved from the ELF — a plain variable
    name or a C path like "g_app.dosing.cfg.pid_kp".

    Args:
        address: Hex address like "0x20000304". Mutually exclusive with symbol.
        symbol: Variable name ("blink") or C path ("app.state.rate"). Requires elf_path.
        elf_path: Path to .elf file for symbol resolution.
        value: Value to write — hex "0xFF" or decimal "255".
        width: Access width — 8, 16, or 32 bits. Auto-detected from symbol size.
        probe: Board nickname, probe nickname, or ST-Link SN. Empty auto-selects
            the only connected board.

    Returns:
        Confirmation of the write operation.
    """
    sn, target_cfg, chipid = resolve_probe_full(probe)
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor,
            lambda: _do_write_memory(address, symbol, elf_path, value, width, sn, target_cfg, chipid),
        ),
        timeout=MEMORY_TIMEOUT + 10,
    )
