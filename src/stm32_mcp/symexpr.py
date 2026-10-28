"""symexpr — resolve arbitrary C expressions (dotted paths, array indexing)
from the firmware ELF via GDB batch.

Lets the agent monitor or inspect nested fields like `g_app.dosing.cfg.pid_kp`
or `tasks[2].lastRunMs` directly, instead of grepping headers and hand-running
GDB to compute addresses. One GDB invocation resolves a whole batch of
expressions; results are cached per ELF (mtime-keyed).
"""

import asyncio
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .toolchain import find_gdb

GDB_TIMEOUT = 20
_executor = ThreadPoolExecutor(max_workers=1)

_EXPR_RE = re.compile(r"^[A-Za-z_]\w*((\.[A-Za-z_]\w*)|(\[\d+\]))*$")

_CACHE: dict = {}

@dataclass
class ResolvedExpr:
    expr: str
    address: int
    size: int
    type_name: str = ""

def is_path_expression(name: str) -> bool:
    """True if the name looks like a C path expression (contains '.' or '[')."""
    name = name.strip()
    return bool(_EXPR_RE.match(name)) and (("." in name) or ("[" in name))

def resolve_expressions(elf_path: str, exprs: list) -> dict:
    """Resolve C path expressions -> {expr: ResolvedExpr | None}.

    None means the expression could not be resolved (unknown symbol, bad path).
    """
    exprs = [e.strip() for e in exprs]
    if not exprs or not elf_path or not os.path.isfile(elf_path):
        return {e: None for e in exprs}

    try:
        mtime = os.path.getmtime(elf_path)
    except OSError:
        return {e: None for e in exprs}

    cache_key = (elf_path, mtime)
    cached = _CACHE.get(cache_key, {})
    result: dict = {}
    missing = []
    for e in exprs:
        if e in cached:
            result[e] = cached[e]
        else:
            missing.append(e)
    if not missing:
        return result

    gdb = find_gdb()
    if not gdb:
        return {**result, **{e: None for e in missing}}

    cmd = [gdb, "-batch", "-q"]
    for e in missing:
        cmd.extend([
            "-ex", f'printf "SLOT {e}\\n"',
            "-ex", f"p/x (unsigned long)&({e})",
            "-ex", f"p (int)sizeof({e})",
            "-ex", f"ptype ({e})",
        ])
    cmd.append(elf_path)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GDB_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return {**result, **{e: None for e in missing}}

    blocks: dict = {}
    current = None
    for line in proc.stdout.splitlines():
        m = re.match(r"^SLOT ([A-Za-z_]\w*(?:\.\w+|\[\d+\])*)\s*$", line.strip())
        if m:
            current = m.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line.strip())

    for e in missing:
        entry = _parse_slot(e, blocks.get(e, []))
        result[e] = entry
        cached[e] = entry

    _CACHE[cache_key] = cached
    return result

def _parse_slot(expr: str, lines: list) -> ResolvedExpr | None:
    """Parse one slot's lines -> ResolvedExpr | None."""
    addr, size, type_name = None, None, ""
    for line in lines:
        m = re.match(r"^\$\d+\s*=\s*(0x[0-9a-fA-F]+)", line)
        if m and addr is None:
            addr = int(m.group(1), 16)
            continue
        m = re.match(r"^\$\d+\s*=\s*(-?\d+)\s*$", line)
        if m and size is None and addr is not None:
            size = int(m.group(1))
            continue
        if line.startswith("type =") and not type_name:
            type_name = line[len("type ="):].strip()
    if addr is None or addr == 0:
        return None
    return ResolvedExpr(
        expr=expr,
        address=addr & 0xFFFFFFFF,
        size=size if size and size > 0 else 0,
        type_name=type_name[:200],
    )

def resolve_expression(elf_path: str, expr: str) -> ResolvedExpr | None:
    """Resolve one C path expression (thin wrapper over resolve_expressions)."""
    return resolve_expressions(elf_path, [expr]).get(expr.strip())

def resolve_symbol_flexible(elf_path: str, name: str) -> tuple:
    """Resolve a variable by nm symbol name OR C path expression.

    Returns (address, size) or None — same contract as debug_tools._resolve_symbol.
    """
    from .debug_tools import _resolve_symbol

    name = name.strip()
    hit = _resolve_symbol(elf_path, name)
    if hit is not None:
        return hit
    if not is_path_expression(name):
        return None
    resolved = resolve_expression(elf_path, name)
    if resolved is None or resolved.address == 0:
        return None
    return resolved.address, resolved.size or 4

async def symbol_info(elf_path: str, expressions: str) -> str:
    """Resolve C expressions against the firmware ELF — the one-call replacement
    for grepping headers and hand-running GDB.

    Args:
        elf_path: Path to the firmware .elf.
        expressions: A single expression or a JSON array of them. Accepts
            plain symbols ("g_app") and C paths with dots and array indices
            ("g_app.scheduler.taskCount", "g_app.scheduler.tasks[3].lastRunMs").

    Returns:
        One line per expression: address, size, and type. Type tells float vs
        int vs enum vs struct — use it to pick the right live_memory/display
        treatment. Struct-typed entries note that sub-fields resolve as
        "<expr>.<field>".
    """
    import json as _json

    expressions = expressions.strip()
    try:
        exprs = _json.loads(expressions)
        if not isinstance(exprs, list):
            exprs = [expressions]
    except (ValueError, TypeError):
        exprs = [expressions]
    exprs = [str(e).strip() for e in exprs if str(e).strip()]
    if not exprs:
        return "ERROR: No expressions given."
    if not os.path.isfile(elf_path):
        return f"ERROR: ELF file not found: {elf_path}"

    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(
        _executor, lambda: resolve_expressions(elf_path, exprs),
    )

    lines = []
    for e in exprs:
        r = results.get(e)
        if r is None:
            lines.append(f"{e}: NOT FOUND (check spelling — static locals are not visible)")
        else:
            type_part = f"  type: {r.type_name}" if r.type_name else ""
            lines.append(f"{e}: 0x{r.address:08X}  size: {r.size or '?'}{type_part}")
    lines.append(
        "\nStructs: pass sub-fields as new expressions (e.g. \""
        + exprs[0] + ".<field>\") or monitor them directly with "
        "live_memory_start (paths are accepted there too)."
    )
    return "\n".join(lines)
