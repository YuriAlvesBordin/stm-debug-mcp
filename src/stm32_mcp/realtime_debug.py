"""realtime_debug — interactive CPU debugging via a persistent OpenOCD TCL session.

Complements live_memory (non-intrusive variable polling) with full CPU control:
halt / resume / step / reset, hardware breakpoints, data watchpoints, core
registers, wait-for-breakpoint, and a heuristic stack-scan backtrace.

All tool families share one OpenOCD process per probe (see ocd_pool): debug
sessions hold a lease on it, live_memory and one-shot tools (memory reads,
flash) run on the same connection. No connect/disconnect juggling between
tools is needed. Note: halting the core (breakpoints, debug_halt) naturally
affects anything reading the target — that is CPU state, not connection
contention.
"""

import asyncio
import os
import re
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import ocd_pool
from .board_map import resolve_probe_full
from .debug_tools import (
    _ocd_read_cmd,
    _ocd_write_cmd,
    _width_from_size,
    parse_write_value,
)
from .hexfmt import annotate_hex
from .live_memory import _tcl_cmd
from .struct_layout import expand_struct
from .symexpr import resolve_symbol_flexible
from .toolchain import find_addr2line, find_nm

_executor = ThreadPoolExecutor(max_workers=2)

CONNECT_TIMEOUT = 5.0
CMD_TIMEOUT = 5.0
DEFAULT_WAIT_TIMEOUT_MS = 5000
MAX_STEP_COUNT = 100
BACKTRACE_WORDS = 64

@dataclass
class DebugSession:
    session_id: str
    sn: str
    target_cfg: str
    chipid: int
    elf_path: str

    process: subprocess.Popen | None = None
    tcl_sock: socket.socket | None = None
    pool_conn: ocd_pool.SharedOcd | None = None
    target_name: str = ""
    text_symbols: list = field(default_factory=list)
    data_symbols: list = field(default_factory=list)
    breakpoints: dict = field(default_factory=dict)
    watchpoints: dict = field(default_factory=dict)
    wp_specs: dict = field(default_factory=dict)
    start_time: float = 0.0

_debug_sessions: dict[str, DebugSession] = {}

def _get_session(session_id: str) -> DebugSession | None:
    return _debug_sessions.get(session_id)

_NM_CACHE: dict = {}

def _load_nm_symbols(elf_path: str, retries: int = 2) -> dict:
    """One nm pass over the ELF -> {types_string: sorted [(addr, size, name)]}.

    Cached per ELF path (mtime-keyed); nm occasionally fails to spawn under
    load, so retry rather than silently running a symbol-less session.
    """
    try:
        mtime = os.path.getmtime(elf_path)
    except OSError:
        return {}
    cached = _NM_CACHE.get(elf_path)
    if cached and cached[0] == mtime:
        return cached[1]

    nm = find_nm()
    if not nm:
        return {}

    for attempt in range(retries + 1):
        try:
            result = subprocess.run(
                [nm, "-S", "--defined-only", elf_path],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        text, data = [], []
        for line in result.stdout.splitlines():
            parts = line.strip().split()

            if len(parts) >= 4:
                try:
                    entry = (int(parts[0], 16), int(parts[1], 16), parts[3])
                except ValueError:
                    continue
                if parts[2] in ("t", "T"):
                    text.append(entry)
                elif parts[2] in ("b", "B", "d", "D"):
                    data.append(entry)
        text.sort()
        data.sort()
        out = {"tT": text, "bBdD": data}
        _NM_CACHE[elf_path] = (mtime, out)
        return out
    return {}

def _load_text_symbols(elf_path: str, types: str = "tT") -> list:
    """Sorted list of (addr, size, name) for nm symbols of the given types."""
    return _load_nm_symbols(elf_path).get(types, [])

def _nearest_symbol(symbols: list, addr: int) -> tuple[str, int] | None:
    """Return (name, addr - symbol_addr) for the closest symbol at or below addr."""
    if not symbols:
        return None
    lo, hi = 0, len(symbols)
    while lo < hi:
        mid = (lo + hi) // 2
        if symbols[mid][0] <= addr:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None
    sym_addr, sym_size, name = symbols[lo - 1]
    return name, addr - sym_addr

def _addr2line_map(elf_path: str, addrs: list) -> dict:
    """Batch addr2line lookup: {addr: "file:line"} (empty dict if unavailable)."""
    a2l = find_addr2line()
    if not a2l or not elf_path or not os.path.isfile(elf_path) or not addrs:
        return {}
    try:
        result = subprocess.run(
            [a2l, "-e", elf_path, "-f", "-C"] + [f"0x{a:X}" for a in addrs],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}

    lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    elf_dir = os.path.dirname(elf_path)
    out = {}
    for i, addr in enumerate(addrs):
        func = lines[i * 2] if i * 2 < len(lines) else "?"
        loc = lines[i * 2 + 1] if i * 2 + 1 < len(lines) else "??"
        if loc and loc != "??" and ":?" not in loc and loc.split(":")[0] not in ("??", "?"):
            loc = re.sub(r"\s*\(discriminator \d+\)", "", loc)
            if elf_dir and os.path.isabs(loc.split(":")[0]):
                try:
                    loc = f"{os.path.relpath(loc.split(':')[0], elf_dir)}:{loc.split(':', 1)[1]}"
                except ValueError:
                    pass
            out[addr] = f"{func} at {loc}" if func and func != "??" else loc
    return out

def _symbols_for(session: DebugSession, addr: int) -> list:
    """Pick the symbol table matching the address space (RAM -> data, flash -> code)."""
    if addr >= 0x20000000 and session.data_symbols:
        return session.data_symbols
    return session.text_symbols

def _fmt_location(session: DebugSession, addr: int) -> str:
    """Format an address as '0x... in func+0xoff (func at file:line)'."""
    addr &= ~1
    parts = [f"0x{addr:08X}"]
    nearest = _nearest_symbol(_symbols_for(session, addr), addr)
    if nearest:
        name, off = nearest
        inside = off < 0x2000
        parts.append(f"in {name}+0x{off:X}" + ("" if inside else " (?)"))
    a2l = _addr2line_map(session.elf_path, [addr]).get(addr)
    if a2l:
        parts.append(f"({a2l})")
    return " ".join(parts)

def _resolve_location(elf_path: str, location: str) -> int:
    """Resolve a breakpoint location: symbol name or hex address. Raises ValueError."""
    location = location.strip()
    if not location:
        raise ValueError("location is required (symbol name or hex address).")
    if location.lower().startswith("0x"):
        try:
            return int(location, 16)
        except ValueError:
            raise ValueError(f"Invalid hex address: {location}")
    if not elf_path:
        raise ValueError("This session was started without elf_path — pass a hex address instead.")
    result = resolve_symbol_flexible(elf_path, location)
    if result is None:
        raise ValueError(f'Symbol "{location}" not found in {os.path.basename(elf_path)}.')
    return result[0]

def _s_cmd(session: DebugSession, cmd: str, timeout: float | None = None) -> str:
    """TCL command on the session socket, serialized with the pool connection
    so it never interleaves with flash/one-shot commands on the same OpenOCD."""
    sock = session.tcl_sock
    if timeout is not None:
        sock.settimeout(timeout)
    try:
        with session.pool_conn.lock:
            return _tcl_cmd(sock, cmd)
    finally:
        if timeout is not None:
            sock.settimeout(CONNECT_TIMEOUT)

def _checked(session: DebugSession, cmd: str, timeout: float = CMD_TIMEOUT) -> str:
    """Run a state-changing TCL command; return result or raise RuntimeError on error."""
    sock = session.tcl_sock
    sock.settimeout(timeout)
    try:
        wrapped = (
            f"if {{[catch {{{cmd}}} _dbg_err]}} "
            f'{{ return "ERR: $_dbg_err" }} else {{ return "OK" }}'
        )
        with session.pool_conn.lock:
            resp = _tcl_cmd(sock, wrapped)
    finally:
        sock.settimeout(CONNECT_TIMEOUT)
    if resp.startswith("ERR:"):
        detail = resp[4:].strip() or "command failed with no error message"
        raise RuntimeError(f"{detail} (cmd: {cmd})")
    return resp

def _target_state(session: DebugSession) -> str:
    """Current target state: 'halted', 'running', etc.

    The target object has no 'curstate' subcommand on the TCL RPC port in the
    ST build — the state is the last column of the current-target row in the
    'targets' table, refreshed by 'poll'.
    """
    try:
        _s_cmd(session, "poll")
    except (RuntimeError, OSError, ConnectionError):
        pass
    listing = _s_cmd(session, "targets")
    for line in listing.splitlines():
        if re.match(r"\s*\d+\*", line):
            cols = line.split()
            if cols:
                return cols[-1]
    m = re.search(r"\b(halted|running|reset|unknown)\b", listing)
    return m.group(1) if m else "unknown"

def _reg_dump(session: DebugSession) -> dict:
    """Read all core registers -> {name_lower: int}."""
    resp = _s_cmd(session, "reg")
    regs = {}
    for m in re.finditer(
        r"^\s*(?:\(\d+\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(/\d+\)\s*:\s*(?:0x)?([0-9a-fA-F]+)",
        resp, re.M,
    ):
        regs[m.group(1).lower()] = int(m.group(2), 16)
    return regs

_CORE_ALIASES = {"pc": ("pc", "r15"), "lr": ("lr", "r14"), "sp": ("sp", "r13"),
                 "xpsr": ("xpsr", "xpsr")}

def _core_regs(session: DebugSession) -> dict:
    """{pc, lr, sp, xpsr} values, or {} if unavailable (e.g. core running)."""
    regs = _reg_dump(session)
    out = {}
    for canonical, aliases in _CORE_ALIASES.items():
        for a in aliases:
            if a in regs:
                out[canonical] = regs[a]
                break
    return out

def _halt_report(session: DebugSession) -> str:
    """One-line 'where am I' summary; assumes core is halted."""
    core = _core_regs(session)
    if not core:
        return "  (register read failed)"
    lines = []
    if "pc" in core:
        lines.append(f"  PC:  {_fmt_location(session, core['pc'])}")
    if "lr" in core:
        lines.append(f"  LR:  {_fmt_location(session, core['lr'])}")
    if "sp" in core:
        lines.append(f"  SP:  0x{core['sp']:08X}")
    if "xpsr" in core:
        lines.append(f"  xPSR: 0x{core['xpsr']:08X}")
    return "\n".join(lines)

def _list_bpwp(session: DebugSession) -> tuple[str, str]:
    """Raw OpenOCD breakpoint and watchpoint listing."""
    bps = _s_cmd(session, "bp").strip()
    wps = _s_cmd(session, "wp").strip()
    return bps, wps

def _format_bpwp(bps: str, wps: str) -> str:
    """Format raw OpenOCD bp/wp listings for display."""
    lines = []
    if bps and "0x" in bps:
        lines.append(f"Breakpoints: {' '.join(bps.split())}")
    if wps and "0x" in wps:
        lines.append(f"Watchpoints: {' '.join(wps.split())}")
    return "\n".join(lines) if lines else "(none set)"

def _sync_bpwp_lists(session: DebugSession) -> None:
    """Refresh session breakpoint/watchpoint maps from OpenOCD.

    Listing entries are line-based (e.g. 'Hardware breakpoint(IVA): addr=0x...,
    len=0x2, num=0'); the FIRST hex value on each line is the address —
    grabbing every hex would pick up len/num as phantom addresses.
    """
    bps, wps = _list_bpwp(session)
    session.breakpoints = {}
    session.watchpoints = {}
    for source, target, strip_bit in (
        (bps, session.breakpoints, True),
        (wps, session.watchpoints, False),
    ):
        for line in source.splitlines():
            m = re.search(r"0x([0-9a-fA-F]+)", line)
            if not m:
                continue
            addr = int(m.group(1), 16)
            if strip_bit:
                addr &= ~1
            target[addr] = _fmt_location(session, addr)

def _stack_backtrace(session: DebugSession, words: int) -> str:
    """Heuristic backtrace: PC/LR then scan stack for thumb return addresses."""
    core = _core_regs(session)
    if "pc" not in core or "sp" not in core:
        return "ERROR: Could not read PC/SP — is the core halted?"

    lines = [f"#0  {_fmt_location(session, core['pc'])}"]
    lr_addr = core.get("lr", 0) & ~1
    if "lr" in core and lr_addr:
        lines.append(f"#1  {_fmt_location(session, core['lr'])}  (LR)")

    if not session.text_symbols:
        lines.append("  (no ELF symbols loaded — start session with elf_path for symbol names)")
        return "\n".join(lines)

    sym_lo = session.text_symbols[0][0]
    sym_hi = session.text_symbols[-1][0] + session.text_symbols[-1][1]

    sp = core["sp"] & ~3
    read_cmd = _ocd_read_cmd(32)

    stack_vals = []
    while words >= 8:
        try:
            resp = _s_cmd(session, f"{read_cmd} 0x{sp:08X} {words}")
        except (OSError, ConnectionError) as e:
            lines.append(f"  (stack scan failed: {e})")
            return "\n".join(lines)
        for line in resp.splitlines():
            m = re.match(r"0x([0-9a-fA-F]+):\s+(.*)", line.strip())
            if m:
                for v in m.group(2).split():
                    if re.match(r"^[0-9a-fA-F]{8}$", v):
                        stack_vals.append(int(v, 16))
        if stack_vals:
            break
        words //= 2

    frames = []
    last_sym = None
    for v in stack_vals:
        if not (v & 1) or not (sym_lo <= (v & ~1) <= sym_hi):
            continue
        if (v & ~1) == lr_addr:
            continue
        nearest = _nearest_symbol(session.text_symbols, v & ~1)
        if not nearest:
            continue
        name, off = nearest
        if name == last_sym:
            continue
        last_sym = name
        frames.append((v & ~1, name, off))
        if len(frames) >= 12:
            break

    a2l = _addr2line_map(session.elf_path, [f[0] for f in frames])
    for i, (addr, name, off) in enumerate(frames, start=len(lines)):
        loc = f" ({a2l[addr]})" if addr in a2l else ""
        lines.append(f"#{i}  0x{addr:08X} in {name}+0x{off:X}{loc}")

    if len(lines) <= 1:
        lines.append("  (no return addresses found in the scanned stack window)")
    return "\n".join(lines)

def _do_session_start(sn: str, target_cfg: str, chipid: int) -> tuple:
    """Blocking session start — runs in executor thread."""
    conn = ocd_pool.acquire_lease(sn, target_cfg, chipid)
    sock = ocd_pool.open_socket(conn)
    return conn, sock

def _do_session_stop(session: DebugSession, resume_first: bool) -> str:
    """Blocking session stop — runs in executor thread.

    Releases the session's lease on the shared OpenOCD; the connection stays
    up for memory reads, flash and live_memory.
    """
    try:
        if resume_first and _target_state(session) == "halted":
            _checked(session, "resume")
    except (RuntimeError, OSError, ConnectionError):
        pass
    try:
        session.tcl_sock.close()
    except OSError:
        pass
    session.tcl_sock = None
    ocd_pool.release_lease(session.pool_conn)

async def debug_session_start(probe: str = "", elf_path: str = "") -> str:
    """Start a real-time debug session via a persistent OpenOCD connection.

    Enables CPU control (halt/resume/step/reset), hardware breakpoints,
    watchpoints, register access, and stack inspection through the
    debug_* tools. One session per probe. The session shares the probe's
    pooled OpenOCD connection with memory reads, flash and live_memory —
    no need to stop it before using other tools.

    Args:
        probe: Board nickname, probe nickname, or ST-Link SN.
        elf_path: Path to the firmware .elf — enables symbol names for
            breakpoints and source locations in halt reports. Recommended.

    Returns:
        Session ID (pass to all other debug_* tools) and core state.
    """
    sn, target_cfg, chipid = resolve_probe_full(probe)
    if not sn:
        return "ERROR: Could not resolve probe. Use stm32_list_probes to see connected boards."
    if target_cfg == "" and chipid == -1:
        return f"ERROR: Probe {sn} has no target MCU attached."
    if not target_cfg:
        return f"ERROR: Unknown chip type for probe {sn}. Use stm32_board_info first."

    existing = [s for s in _debug_sessions.values() if s.sn == sn]
    if existing:
        return (f"ERROR: Debug session {existing[0].session_id} already open on this probe. "
                f"Use it, or debug_session_stop it first.")

    if elf_path and not os.path.isfile(elf_path):
        return f"ERROR: ELF file not found: {elf_path}"

    loop = asyncio.get_event_loop()
    try:
        conn, sock = await loop.run_in_executor(
            _executor, lambda: _do_session_start(sn, target_cfg, chipid),
        )
    except (FileNotFoundError, RuntimeError) as e:
        return f"ERROR: {e}"

    session = DebugSession(
        session_id=uuid.uuid4().hex[:8],
        sn=sn, target_cfg=target_cfg, chipid=chipid, elf_path=elf_path,
        process=conn.proc, tcl_sock=sock, pool_conn=conn,
        start_time=time.monotonic(),
    )

    try:
        listing = _s_cmd(session, "targets")
        m = re.search(r"^\s*\d+\*\s+(\S+)", listing, re.M)
        session.target_name = m.group(1) if m else "target0"
    except (OSError, ConnectionError):
        session.target_name = "target0"
    session.text_symbols = _load_text_symbols(elf_path, "tT")
    session.data_symbols = _load_text_symbols(elf_path, "bBdD")
    if elf_path and not session.text_symbols and not session.data_symbols:

        _NM_CACHE.pop(elf_path, None)
        session.text_symbols = _load_text_symbols(elf_path, "tT")
        session.data_symbols = _load_text_symbols(elf_path, "bBdD")

    _debug_sessions[session.session_id] = session

    state = "unknown"
    try:
        state = _target_state(session)
    except (OSError, ConnectionError, RuntimeError):
        pass

    sym_count = len(session.text_symbols)
    return (
        f"Debug session started.\n"
        f"Session ID: {session.session_id}\n"
        f"Probe: {sn}\n"
        f"Target state: {state}\n"
        f"ELF: {elf_path or '(none — addresses only)'}"
        + (f" ({sym_count} code symbols)" if sym_count else "") + "\n"
        "This session shares the probe's OpenOCD connection with memory "
        "reads, flash and live_memory — you can use them without stopping it."
    )

async def debug_session_stop(session_id: str, resume_first: bool = True) -> str:
    """Stop a debug session (releases its lease on the shared OpenOCD connection).

    Args:
        session_id: Session ID from debug_session_start.
        resume_first: If the core is halted, resume it before releasing so
            firmware keeps running (default true). Set false to leave the core
            halted.

    Returns:
        Confirmation with session duration.
    """
    session = _debug_sessions.pop(session_id, None)
    if session is None:
        available = list(_debug_sessions.keys())
        if available:
            return f"ERROR: Session '{session_id}' not found. Active sessions: {', '.join(available)}"
        return f"ERROR: Session '{session_id}' not found. No active debug sessions."

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _executor, lambda: _do_session_stop(session, resume_first),
    )

    duration = time.monotonic() - session.start_time
    core_note = "resumed before release" if resume_first else "left halted"
    return (
        f"Debug session {session_id} stopped ({core_note}).\n"
        f"Duration: {duration:.1f}s"
    )

async def debug_status(session_id: str) -> str:
    """Report debug session status: core state, location, breakpoints, watchpoints.

    Args:
        session_id: Session ID from debug_session_start.

    Returns:
        Core state (halted/running), PC/LR/SP with function and source line
        when halted, and the current breakpoint/watchpoint lists.
    """
    session = _get_session(session_id)
    if session is None:
        return "ERROR: Unknown session. Use debug_session_start first."

    def _work() -> str:
        try:
            state = _target_state(session)
        except (OSError, ConnectionError) as e:
            return (f"ERROR: OpenOCD session is dead ({e}). "
                    f"Start a new session with debug_session_start.")
        _sync_bpwp_lists(session)

        lines = [f"Session {session_id} — target state: {state}"]
        if state == "halted":
            lines.append(_halt_report(session))
        else:
            lines.append("  (core running — halt with debug_halt to inspect; "
                         "RAM is still readable via debug_mem)")
        lines.append("Breakpoints/Watchpoints:")
        bps, wps = _list_bpwp(session)
        listed = _format_bpwp(bps, wps)
        lines.append(f"  {listed}" if "\n" not in listed else listed)
        return "\n".join(lines)

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(_executor, _work), timeout=15)
    except asyncio.TimeoutError:
        return "ERROR: Status query timed out."

async def debug_halt(session_id: str) -> str:
    """Halt the CPU and report where it stopped.

    Args:
        session_id: Session ID from debug_session_start.

    Returns:
        Core state and PC/LR/SP/xPSR with function name and source line.
    """
    session = _get_session(session_id)
    if session is None:
        return "ERROR: Unknown session. Use debug_session_start first."

    def _work() -> str:
        try:
            if _target_state(session) == "halted":
                return "Core already halted.\n" + _halt_report(session)
            _checked(session, "halt")
            state = _target_state(session)
            if state != "halted":
                return f"ERROR: halt issued but target state is '{state}'."
            return "Core halted.\n" + _halt_report(session)
        except (RuntimeError, OSError, ConnectionError) as e:
            return f"ERROR: {e}"

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(_executor, _work), timeout=15)
    except asyncio.TimeoutError:
        return "ERROR: Halt timed out."

async def debug_resume(session_id: str) -> str:
    """Resume execution of a halted core.

    Args:
        session_id: Session ID from debug_session_start.

    Returns:
        Confirmation, or notice if the core was already running.
    """
    session = _get_session(session_id)
    if session is None:
        return "ERROR: Unknown session. Use debug_session_start first."

    def _work() -> str:
        try:
            state = _target_state(session)
            if state != "halted":
                return f"Core already {state} — nothing to resume."
            _checked(session, "resume")
            new_state = _target_state(session)
            return f"Core resumed (state: {new_state})."
        except (RuntimeError, OSError, ConnectionError) as e:
            return f"ERROR: {e}"

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(_executor, _work), timeout=15)
    except asyncio.TimeoutError:
        return "ERROR: Resume timed out."

async def debug_step(session_id: str, count: int = 1) -> str:
    """Single-step the halted core by N instructions.

    Args:
        session_id: Session ID from debug_session_start.
        count: Number of instructions to step (1-100, default 1).

    Returns:
        New PC location after stepping (function name and source line).
    """
    session = _get_session(session_id)
    if session is None:
        return "ERROR: Unknown session. Use debug_session_start first."
    count = max(1, min(int(count), MAX_STEP_COUNT))

    def _work() -> str:
        try:
            if _target_state(session) != "halted":
                return "ERROR: Core is running — halt it first (debug_halt)."
            for _ in range(count):
                _checked(session, "step", timeout=10.0)
            core = _core_regs(session)
            pc = core.get("pc")
            where = _fmt_location(session, pc) if pc else "(PC unreadable)"
            return f"Stepped {count} instruction(s). Now at: {where}"
        except (RuntimeError, OSError, ConnectionError) as e:
            return f"ERROR: {e}"

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(_executor, _work),
                                      timeout=15 + count * 2)
    except asyncio.TimeoutError:
        return "ERROR: Step timed out."

async def debug_reset(session_id: str, halt: bool = False) -> str:
    """Reset the target (system reset).

    Args:
        session_id: Session ID from debug_session_start.
        halt: false (default) = reset and run firmware; true = reset and halt
            at the reset vector (startup code inspection).

    Returns:
        New target state.
    """
    session = _get_session(session_id)
    if session is None:
        return "ERROR: Unknown session. Use debug_session_start first."

    def _work() -> str:
        try:
            _checked(session, "reset halt" if halt else "reset run", timeout=10.0)

            rearm_note = ""
            if session.wp_specs:
                was_running = _target_state(session) != "halted"
                if was_running:
                    _checked(session, "halt")
                _rearm_watchpoints(session)
                if was_running:
                    _checked(session, "resume")
                rearm_note = f" ({len(session.wp_specs)} watchpoint(s) re-armed)"
            state = _target_state(session)
            mode = "reset + halt" if halt else "reset + run"
            extra = ""
            if halt:
                extra = "\n" + _halt_report(session)
            return f"Target {mode}. State: {state}.{rearm_note}{extra}"
        except (RuntimeError, OSError, ConnectionError) as e:
            return f"ERROR: {e}"

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(_executor, _work), timeout=20)
    except asyncio.TimeoutError:
        return "ERROR: Reset timed out."

async def debug_wait_halt(session_id: str, timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS) -> str:
    """Block until the core halts (e.g. breakpoint hit), then report where.

    Use after debug_resume when a breakpoint is armed. Returns the halt
    location (PC/LR/SP/xPSR with function and source line) or a timeout note.

    Args:
        session_id: Session ID from debug_session_start.
        timeout_ms: Give up after this many milliseconds (default 5000).

    Returns:
        Halt report, or timeout message with current state.
    """
    session = _get_session(session_id)
    if session is None:
        return "ERROR: Unknown session. Use debug_session_start first."
    timeout_ms = max(500, min(int(timeout_ms), 60000))

    def _work() -> str:
        try:
            if _target_state(session) == "halted":
                return "Core is already halted.\n" + _halt_report(session)

            try:
                _checked(session, f"wait_halt {timeout_ms}", timeout=timeout_ms / 1000.0 + 3.0)
            except RuntimeError:
                pass
            if _target_state(session) == "halted":
                hit_hint = ""
                core = _core_regs(session)
                pc = core.get("pc")
                if pc is not None:
                    for bp_addr in session.breakpoints:
                        if (pc & ~1) - 2 <= bp_addr <= (pc & ~1):
                            hit_hint = f"\nLikely breakpoint hit: {session.breakpoints[bp_addr]}"
                            break
                return "Core halted (breakpoint or fault).\n" + _halt_report(session) + hit_hint
            return f"Core still running after {timeout_ms}ms — no breakpoint hit."
        except (OSError, ConnectionError) as e:
            return f"ERROR: {e}"

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(_executor, _work),
                                      timeout=timeout_ms / 1000.0 + 15)
    except asyncio.TimeoutError:
        return f"Core still running after {timeout_ms}ms — no breakpoint hit."

async def debug_breakpoint(session_id: str, action: str = "list", location: str = "") -> str:
    """Manage hardware breakpoints (Flash Patch & Breakpoint unit).

    Args:
        session_id: Session ID from debug_session_start.
        action: "set", "clear", "clear_all", or "list" (default).
        location: Function name or hex address — required for set/clear.
            Function names resolve via the session's ELF.

    Returns:
        Breakpoint list after the operation.
    """
    session = _get_session(session_id)
    if session is None:
        return "ERROR: Unknown session. Use debug_session_start first."
    action = action.strip().lower()

    def _work() -> str:
        try:
            if action == "list":
                pass
            elif action == "set":
                addr = _resolve_location(session.elf_path, location)
                _checked(session, f"bp 0x{addr & ~1:08X} 2 hw")
            elif action == "clear":
                addr = _resolve_location(session.elf_path, location)
                _checked(session, f"rbp 0x{addr & ~1:08X}")
            elif action == "clear_all":
                _sync_bpwp_lists(session)
                failures = []
                for bp_addr in sorted(session.breakpoints):
                    try:
                        _checked(session, f"rbp 0x{bp_addr:08X}")
                    except RuntimeError as e:
                        failures.append(f"0x{bp_addr:08X}: {e}")
                if failures:
                    return "ERROR: some breakpoints could not be removed:\n" + "\n".join(failures)
            else:
                return f'ERROR: Invalid action "{action}". Use set, clear, clear_all, or list.'

            _sync_bpwp_lists(session)
            if not session.breakpoints:
                return "Breakpoints: (none set)"
            lines = ["Breakpoints:"]
            for bp_addr in sorted(session.breakpoints):
                lines.append(f"  0x{bp_addr:08X}  {_fmt_location(session, bp_addr)}")
            return "\n".join(lines)
        except ValueError as e:
            return f"ERROR: {e}"
        except (RuntimeError, OSError, ConnectionError) as e:
            return (f"ERROR: {e} — if the core is running, try debug_halt first; "
                    f"some probes refuse FPB writes while running.")

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(_executor, _work), timeout=15)
    except asyncio.TimeoutError:
        return "ERROR: Breakpoint operation timed out."

def _rearm_watchpoints(session: DebugSession) -> None:
    """Re-program all tracked watchpoints (DWT registers are wiped by reset).

    Requires the core to be halted; leaves the core halted.
    """
    for wp_addr, (width, acc, value) in session.wp_specs.items():
        cmd = f"wp 0x{wp_addr:08X} {width} {acc}"
        if value:
            cmd += f" 0x{value}"
        try:
            _checked(session, cmd)
        except RuntimeError:
            pass

async def debug_watchpoint(
    session_id: str,
    action: str = "list",
    location: str = "",
    width: int = 4,
    access: str = "rw",
    value: str = "",
) -> str:
    """Manage data watchpoints (DWT comparators — usually only a few per MCU).

    Args:
        session_id: Session ID from debug_session_start.
        action: "set", "clear", "clear_all", or "list" (default).
        location: Variable name (ELF) or hex address — required for set/clear.
        width: Access size in bytes: 1, 2, or 4 (default 4).
        access: "r", "w", or "rw" (either direction, default).
        value: If set (e.g. "5"), break only when the watched value matches.

    Returns:
        Watchpoint list after the operation.
    """
    session = _get_session(session_id)
    if session is None:
        return "ERROR: Unknown session. Use debug_session_start first."
    action = action.strip().lower()

    def _work() -> str:
        try:
            if action == "list":
                pass
            elif action == "set":
                addr = _resolve_location(session.elf_path, location)
                if width not in (1, 2, 4):
                    return "ERROR: width must be 1, 2, or 4 bytes."
                access_in = access.strip().lower()
                if access_in == "rw":
                    acc = "a"
                elif access_in in ("r", "w"):
                    acc = access_in
                else:
                    return 'ERROR: access must be "r", "w", or "rw".'
                cmd = f"wp 0x{addr:08X} {width} {acc}"
                if value:
                    try:
                        cmd += f" 0x{int(value, 0):X}"
                    except ValueError:
                        return f"ERROR: Invalid value: {value}"

                was_running = _target_state(session) != "halted"
                if was_running:
                    _checked(session, "halt")
                try:
                    _checked(session, cmd)
                    session.wp_specs[addr] = (width, acc, str(int(value, 0), 16) if value else "")
                finally:
                    if was_running:
                        _checked(session, "resume")
            elif action == "clear":
                addr = _resolve_location(session.elf_path, location)
                was_running = _target_state(session) != "halted"
                if was_running:
                    _checked(session, "halt")
                try:
                    _checked(session, f"rwp 0x{addr:08X}")
                    session.wp_specs.pop(addr, None)
                finally:
                    if was_running:
                        _checked(session, "resume")
            elif action == "clear_all":
                _sync_bpwp_lists(session)
                was_running = _target_state(session) != "halted"
                if was_running and session.watchpoints:
                    _checked(session, "halt")
                failures = []
                for wp_addr in sorted(session.watchpoints):
                    try:
                        _checked(session, f"rwp 0x{wp_addr:08X}")
                        session.wp_specs.pop(wp_addr, None)
                    except RuntimeError as e:
                        failures.append(f"0x{wp_addr:08X}: {e}")
                if was_running and session.watchpoints:
                    _checked(session, "resume")
                if failures:
                    return "ERROR: some watchpoints could not be removed:\n" + "\n".join(failures)
            else:
                return f'ERROR: Invalid action "{action}". Use set, clear, clear_all, or list.'

            _sync_bpwp_lists(session)
            if not session.watchpoints:
                return "Watchpoints: (none set)"
            lines = ["Watchpoints:"]
            for wp_addr in sorted(session.watchpoints):
                lines.append(f"  0x{wp_addr:08X}  {_fmt_location(session, wp_addr)}")
            return "\n".join(lines)
        except ValueError as e:
            return f"ERROR: {e}"
        except (RuntimeError, OSError, ConnectionError) as e:
            return (f"ERROR: {e} — DWT watchpoint comparators are limited "
                    f"(often 4, sometimes 0 on Cortex-M0/M0+).")

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(_executor, _work), timeout=15)
    except asyncio.TimeoutError:
        return "ERROR: Watchpoint operation timed out."

async def debug_registers(session_id: str, regs: str = "core") -> str:
    """Read core registers while the core is halted.

    Args:
        session_id: Session ID from debug_session_start.
        regs: "core" (default) = PC, LR, SP, xPSR; "all" = full register dump
            (r0-r12, SP, LR, PC, xPSR, MSP/PSP, CONTROL...); or a single
            register name like "r5" or "pc".

    Returns:
        Register values in hex. PC and LR are annotated with function and
        source line when an ELF is loaded.
    """
    session = _get_session(session_id)
    if session is None:
        return "ERROR: Unknown session. Use debug_session_start first."
    regs = regs.strip().lower()

    def _work() -> str:
        try:
            state = _target_state(session)
            if state != "halted":
                return f"ERROR: Core is {state} — register values require a halted core (debug_halt)."
            if regs == "core":
                core = _core_regs(session)
                if not core:
                    return "ERROR: Could not read registers."
                return _halt_report(session)
            dump = _reg_dump(session)
            if not dump:
                return "ERROR: Could not read registers."
            if regs != "all" and regs in dump:
                val = dump[regs]
                note = ""
                if regs in ("pc", "lr"):
                    note = f"  {_fmt_location(session, val)}"
                return f"{regs}: 0x{val:08X}{note}"
            lines = [f"{name}: 0x{val:08X}" for name, val in sorted(dump.items())]
            return "\n".join(lines)
        except (RuntimeError, OSError, ConnectionError) as e:
            return f"ERROR: {e}"

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(_executor, _work), timeout=15)
    except asyncio.TimeoutError:
        return "ERROR: Register read timed out."

async def debug_mem(
    session_id: str,
    action: str = "read",
    address: str = "",
    symbol: str = "",
    value: str = "",
    width: int = 32,
    count: int = 1,
) -> str:
    """Read or write RAM/registers through the debug session.

    Works while the core is running (real-time) as well as halted — unlike
    stm32_read_memory this does not launch a second OpenOCD.

    Args:
        session_id: Session ID from debug_session_start.
        action: "read" (default) or "write".
        address: Hex address like "0x20000304". Mutually exclusive with symbol.
        symbol: Variable name resolved from the session ELF. Structs
            (size > 4 bytes) are auto-expanded into per-field reads.
        value: Value to write (hex "0xFF" or decimal) — write only.
        width: Access width — 8, 16, or 32 bits (auto from symbol size).
        count: Number of units to read (read only).

    Returns:
        Memory contents as hex values, or write confirmation.
    """
    session = _get_session(session_id)
    if session is None:
        return "ERROR: Unknown session. Use debug_session_start first."
    action = action.strip().lower()
    elf = session.elf_path

    resolved_addr, sym_label, resolved_width, sym_size = 0, "", max(8, min(int(width), 32)), 0
    try:
        if symbol:
            if not elf:
                return "ERROR: Session has no ELF — pass a hex address instead of symbol."
            result = resolve_symbol_flexible(elf, symbol)
            if result is None:
                return f'ERROR: Symbol "{symbol}" not found in {os.path.basename(elf)}.'
            resolved_addr, sym_size = result
            sym_label = symbol
            resolved_width = _width_from_size(sym_size)
        elif address:
            resolved_addr = int(address, 0)
        else:
            return "ERROR: Provide either address or symbol."
    except ValueError as e:
        return f"ERROR: {e}"

    def _work() -> str:
        try:
            if action == "read":

                targets = [(sym_label, resolved_addr, resolved_width)]
                if sym_label and sym_size > 4:
                    fields = expand_struct(elf, sym_label)
                    if fields:
                        targets = [
                            (f"{sym_label}.{f.name}", resolved_addr + f.offset, _width_from_size(f.size))
                            for f in fields
                        ]
                lines = []
                for name, t_addr, t_width in targets:
                    resp = _s_cmd(
                        session,
                        f"{_ocd_read_cmd(t_width)} 0x{t_addr:08X} {max(1, int(count))}",
                    )
                    hexvals = []
                    for line in resp.splitlines():
                        m = re.match(r"0x[0-9a-fA-F]+:\s+(.*)", line.strip())
                        if m:
                            hexvals.extend(
                                v for v in m.group(1).split()
                                if re.fullmatch(r"[0-9a-fA-F]+", v)
                            )
                    lines.append(f"{name} (0x{t_addr:08X}): {annotate_hex(hexvals, t_width) or '(unreadable)'}")
                return "\n".join(lines)
            elif action == "write":
                if not value:
                    return "ERROR: value is required for write."
                write_val, err = parse_write_value(value, resolved_width)
                if err:
                    return f"ERROR: {err}"
                _checked(
                    session,
                    f"{_ocd_write_cmd(resolved_width)} 0x{resolved_addr:08X} 0x{write_val:X}",
                )
                label = f"{sym_label} (0x{resolved_addr:08X})" if sym_label else f"0x{resolved_addr:08X}"
                fmt = {8: "02X", 16: "04X", 32: "08X"}[resolved_width]
                return f"Wrote 0x{write_val:{fmt}} to {label}"
            else:
                return f'ERROR: Invalid action "{action}". Use "read" or "write".'
        except (RuntimeError, OSError, ConnectionError) as e:
            return f"ERROR: {e}"

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(_executor, _work), timeout=15)
    except asyncio.TimeoutError:
        return "ERROR: Memory access timed out."

async def debug_backtrace(session_id: str, words: int = BACKTRACE_WORDS) -> str:
    """Reconstruct an approximate call stack from PC, LR, and a stack scan.

    Requires a halted core and an ELF-loaded session. The stack scan is a
    heuristic: it reports PC, LR, then likely return addresses (thumb addresses
    into code symbols) found in the stack window, in stack order.

    Args:
        session_id: Session ID from debug_session_start.
        words: Stack words to scan (default 64, max 256).

    Returns:
        Approximate call frames, innermost first.
    """
    session = _get_session(session_id)
    if session is None:
        return "ERROR: Unknown session. Use debug_session_start first."
    words = max(8, min(int(words), 256))

    def _work() -> str:
        try:
            if _target_state(session) != "halted":
                return "ERROR: Core is running — halt it first (debug_halt)."
            return _stack_backtrace(session, words)
        except (RuntimeError, OSError, ConnectionError) as e:
            return f"ERROR: {e}"

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(_executor, _work), timeout=20)
    except asyncio.TimeoutError:
        return "ERROR: Backtrace timed out."
