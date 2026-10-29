"""live_memory — continuous background memory monitoring via persistent OpenOCD TCL."""

import asyncio
import collections
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .board_map import resolve_probe_full
from .debug_tools import _ocd_read_cmd, _ocd_write_cmd, _width_from_size, parse_write_value
from .struct_layout import expand_struct
from .symexpr import resolve_symbol_flexible
from .toolchain import (
    _HLA_NEWTAP_FIX,
    _openocd_scripts_dir,
    find_openocd,
    openocd_workarea,
)

_executor = ThreadPoolExecutor(max_workers=2)

TCL_PORT = 6666
TCL_TERMINATOR = b"\x1a"
CONNECT_TIMEOUT = 5.0
RING_BUFFER_SIZE = 100
MIN_INTERVAL_MS = 250

@dataclass
class ResolvedVariable:
    name: str
    address: int
    width: int
    is_float: bool = False
    count: int = 1
    type_name: str = ""
    enum_values: dict | None = None

@dataclass
class LiveMemorySession:
    session_id: str
    variables: list[ResolvedVariable]
    sn: str
    target_cfg: str
    chipid: int
    interval_ms: int
    output_path: str
    elf_path: str = ""

    process: subprocess.Popen | None = None
    tcl_sock: socket.socket | None = None
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    tcl_lock: threading.Lock = field(default_factory=threading.Lock)
    ring_buffer: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=RING_BUFFER_SIZE)
    )
    start_time: float = 0.0
    read_count: int = 0
    error_count: int = 0

_sessions: dict[str, LiveMemorySession] = {}

def _pdeathsig():
    """Preexec hook: ask the kernel to kill OpenOCD if this process dies.

    Without it an orphaned OpenOCD keeps claiming the ST-Link until manually
    killed, breaking every later probe (st-info, flash, live_memory).
    """
    if sys.platform != "linux":
        return None

    def _set():
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, 15)

    return _set

def _start_openocd(sn: str, target_cfg: str, chipid: int) -> tuple[subprocess.Popen, socket.socket]:
    """Start OpenOCD as a persistent subprocess and connect to its TCL port."""
    openocd = find_openocd()
    if not openocd:
        raise FileNotFoundError("OpenOCD not found. Install via: brew install open-ocd")

    cmd = [openocd]

    scripts_dir = _openocd_scripts_dir(openocd)
    if scripts_dir:
        cmd.extend(["-s", scripts_dir])

    cmd.extend([
        "-f", "interface/stlink.cfg",
        "-c", f"adapter serial {sn}",
        "-c", "transport select hla_swd",
    ])

    if scripts_dir:
        cmd.extend(["-c", _HLA_NEWTAP_FIX])

    wa = openocd_workarea(chipid) if chipid else None
    if wa is not None:
        cmd.extend(["-c", f"set WORKAREASIZE 0x{wa:X}"])

    cmd.extend(["-f", f"target/{target_cfg}"])
    cmd.extend(["-c", "init"])

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=_pdeathsig(),
    )

    deadline = time.monotonic() + CONNECT_TIMEOUT
    sock = None

    while time.monotonic() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect(("127.0.0.1", TCL_PORT))
            sock = s
            break
        except (ConnectionRefusedError, OSError):
            s.close()
            if proc.poll() is not None:
                _, stderr = proc.communicate(timeout=2)
                raise RuntimeError(
                    f"OpenOCD exited immediately (code {proc.returncode}):\n{stderr.decode(errors='replace').strip()}"
                )
            time.sleep(0.2)

    if sock is None:
        proc.terminate()
        proc.wait(timeout=5)
        raise RuntimeError(f"Could not connect to OpenOCD TCL port {TCL_PORT} within {CONNECT_TIMEOUT}s")

    return proc, sock

def _tcl_cmd(sock: socket.socket, cmd: str) -> str:
    """Send a command to OpenOCD's TCL interface and return the response."""
    sock.sendall((cmd + "\x1a").encode())

    data = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("OpenOCD TCL connection closed")
        data.extend(chunk)
        if TCL_TERMINATOR in data:

            idx = data.index(TCL_TERMINATOR[0])
            return data[:idx].decode(errors="replace")

def _width_count_from_size(size: int) -> tuple:
    """(width, word count) for a symbol size — 64-bit values read as 2 words."""
    if size == 8:
        return 32, 2
    return _width_from_size(size), 1

def _resolve_variables(variables_json: str, elf_path: str) -> list[ResolvedVariable]:
    """Parse variable specs and resolve symbols to addresses.

    Symbol specs accept plain names ("blink") or C paths with dots and
    array indices ("g_app.dosing.cfg.pid_kp", "tasks[2].lastRunMs") —
    dotted paths resolve via GDB against the ELF.
    """
    try:
        specs = json.loads(variables_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in variables: {e}")

    if not isinstance(specs, list):
        raise ValueError("variables must be a JSON array")

    resolved = []
    for spec in specs:
        if isinstance(spec, str):

            if not elf_path:
                raise ValueError(f'elf_path required to resolve symbol "{spec}"')
            result = resolve_symbol_flexible(elf_path, spec)
            if result is None:
                raise ValueError(f'Symbol "{spec}" not found in {os.path.basename(elf_path)}')
            addr, size = result

            if size > 4:
                fields = expand_struct(elf_path, spec)
                if fields:
                    for f in fields:
                        w, c = _width_count_from_size(f.size)
                        resolved.append(ResolvedVariable(
                            name=f"{spec}.{f.name}",
                            address=addr + f.offset,
                            width=w,
                            count=c,
                            is_float=f.is_float,
                            type_name=f.type_name,
                            enum_values=f.enum_values,
                        ))
                    continue

            w, c = _width_count_from_size(size)
            resolved.append(ResolvedVariable(
                name=spec,
                address=addr,
                width=w,
                count=c,
            ))
        elif isinstance(spec, dict):

            type_hint = spec.get("type", "").strip()
            is_float = type_hint.lower() == "float"

            if "symbol" in spec:
                sym_name = spec["symbol"]
                if not elf_path:
                    raise ValueError(f'elf_path required to resolve symbol "{sym_name}"')
                result = resolve_symbol_flexible(elf_path, sym_name)
                if result is None:
                    raise ValueError(f'Symbol "{sym_name}" not found in {os.path.basename(elf_path)}')
                addr, size = result
                name = spec.get("name", sym_name)
                if "width" in spec:
                    width, count = int(spec["width"]), 1
                else:
                    width, count = _width_count_from_size(size)

                if size > 4 and spec.get("expand", True):
                    fields = expand_struct(elf_path, sym_name)
                    if fields:
                        for f in fields:
                            w, c = _width_count_from_size(f.size)
                            resolved.append(ResolvedVariable(
                                name=f"{name}.{f.name}",
                                address=addr + f.offset,
                                width=w,
                                count=c,
                                is_float=f.is_float,
                                type_name=f.type_name,
                                enum_values=f.enum_values,
                            ))
                        continue

                resolved.append(ResolvedVariable(
                    name=name, address=addr, width=width, count=count,
                    is_float=is_float,
                    type_name=type_hint,
                ))
            elif "address" in spec:
                addr = int(spec["address"], 0)
                name = spec.get("name", f"0x{addr:08X}")
                width = spec.get("width", 32)
                resolved.append(ResolvedVariable(
                    name=name, address=addr, width=width, is_float=is_float,
                    type_name=type_hint,
                ))
            else:
                raise ValueError(f"Variable dict must have 'symbol' or 'address': {spec}")
        else:
            raise ValueError(f"Variable spec must be string or dict, got {type(spec).__name__}")

    return resolved

def _parse_mdw_value(response: str) -> int | None:
    """Parse a hex value from OpenOCD mdw/mdh/mdb output.

    Format: 0x20000304: 00000001
    """
    m = re.search(r'0x[0-9a-fA-F]+:\s+([0-9a-fA-F]+)', response)
    if m:
        return int(m.group(1), 16)
    return None

def _parse_mdw_words(response: str) -> list:
    """Parse all words from mdw output -> [(addr, value), ...] in address order."""
    out = []
    for line in response.splitlines():
        m = re.match(r"0x([0-9a-fA-F]+):\s+(.*)", line.strip())
        if m:
            addr = int(m.group(1), 16)
            for v in m.group(2).split():
                if re.fullmatch(r"[0-9a-fA-F]+", v):
                    out.append((addr, int(v, 16)))
                    addr += 4
    return out

_SIGNED_TYPES = {
    "int8_t", "int16_t", "int32_t", "int64_t",
    "signed char", "short", "int", "long",
}

_BOOL_TYPES = {"_Bool", "bool"}

def _finalize_value(var: ResolvedVariable, raw: int):
    """Interpret a raw word (or word pair) as float/int/enum/bool per the spec."""
    if var.is_float and var.width == 32 and var.count == 1:
        return round(struct.unpack("<f", struct.pack("<I", raw))[0], 6)
    if var.enum_values is not None and var.count == 1:
        return var.enum_values.get(raw, raw)
    if var.type_name in _BOOL_TYPES and var.count == 1:
        return "true" if raw else "false"
    if var.type_name in _SIGNED_TYPES and var.count == 1 and raw >= 1 << (var.width - 1):
        return raw - (1 << var.width)
    return raw

def _build_read_groups(variables: list) -> tuple:
    """Plan one polling pass.

    32-bit single-word variables with nearby addresses are merged into batched
    range reads (one mdw per group instead of one per variable — a struct like
    g_app collapses dozens of round trips into one). Everything else (8/16-bit,
    64-bit pairs) stays a single targeted read.
    """
    singles = [v for v in variables if not (v.width == 32 and v.count == 1)]
    batch = sorted(
        (v for v in variables if v.width == 32 and v.count == 1),
        key=lambda v: v.address,
    )
    groups, cur = [], []
    for v in batch:
        if cur and (
            v.address - (cur[-1].address + 4) > 96
            or v.address + 4 - cur[0].address > 1024
        ):
            groups.append(cur)
            cur = []
        cur.append(v)
    if cur:
        groups.append(cur)
    return groups, singles

def _try_reconnect(session: LiveMemorySession, max_retries: int = 3) -> bool:
    """Try to restart OpenOCD and reconnect TCL socket. Returns True on success."""

    if session.tcl_sock:
        try:
            session.tcl_sock.close()
        except OSError:
            pass
        session.tcl_sock = None
    if session.process:
        try:
            session.process.terminate()
            session.process.wait(timeout=3)
        except Exception:
            try:
                session.process.kill()
            except Exception:
                pass
        session.process = None

    for attempt in range(max_retries):
        if session.stop_event.is_set():
            return False
        delay = (1 << attempt)
        session.stop_event.wait(delay)
        if session.stop_event.is_set():
            return False

        try:
            proc, sock = _start_openocd(session.sn, session.target_cfg, session.chipid)
            session.process = proc
            session.tcl_sock = sock
            return True
        except (FileNotFoundError, RuntimeError):
            session.error_count += 1

    return False

def _run_session(session: LiveMemorySession) -> None:
    """Background daemon thread: poll variables and write JSONL."""
    try:
        output_file = open(session.output_path, "a")
    except OSError:
        session.error_count += 1
        return

    groups, singles = _build_read_groups(session.variables)

    def _poll_once() -> dict:
        """One full read pass; raises ConnectionError on a dead OpenOCD link."""
        values: dict = {}
        for group in groups:
            start = group[0].address
            words = (group[-1].address + 4 - start) // 4
            with session.tcl_lock:
                resp = _tcl_cmd(session.tcl_sock, f"mdw 0x{start:08X} {words}")
            window = dict(_parse_mdw_words(resp))
            for var in group:
                if var.address in window:
                    values[var.name] = _finalize_value(var, window[var.address])
                else:
                    values[var.name] = "ERROR: read failed"
                    session.error_count += 1
        for var in singles:
            with session.tcl_lock:
                resp = _tcl_cmd(
                    session.tcl_sock,
                    f"{_ocd_read_cmd(var.width)} 0x{var.address:08X} {var.count}",
                )
            words_read = [v for _a, v in _parse_mdw_words(resp)]
            if len(words_read) >= var.count:
                raw = words_read[0] | (words_read[1] << 32) if var.count == 2 else words_read[0]
                values[var.name] = _finalize_value(var, raw)
            else:
                values[var.name] = "ERROR: read failed"
                session.error_count += 1
        return values

    try:
        while not session.stop_event.is_set():
            loop_start = time.monotonic()
            elapsed_s = loop_start - session.start_time

            try:
                values = _poll_once()
            except (OSError, ConnectionError):

                session.error_count += 1
                if not _try_reconnect(session):
                    return
                continue

            entry = {
                "t": round(time.time(), 3),
                "elapsed_s": round(elapsed_s, 3),
                "values": values,
            }
            session.ring_buffer.append(entry)
            session.read_count += 1

            try:
                output_file.write(json.dumps(entry) + "\n")
                output_file.flush()
            except OSError:
                pass

            loop_elapsed = time.monotonic() - loop_start
            remaining = (session.interval_ms / 1000.0) - loop_elapsed
            if remaining > 0:
                session.stop_event.wait(remaining)

    except (OSError, ConnectionError):
        session.error_count += 1
    finally:
        output_file.close()

        if session.tcl_sock:
            try:
                session.tcl_sock.close()
            except OSError:
                pass
        if session.process:
            try:
                session.process.terminate()
                session.process.wait(timeout=5)
            except Exception:
                try:
                    session.process.kill()
                except Exception:
                    pass

async def live_memory_start(
    variables: str,
    elf_path: str = "",
    probe: str = "",
    interval_ms: int = 1000,
    output_path: str = "",
) -> str:
    """Start continuous background memory monitoring via SWD.

    Launches a persistent OpenOCD connection and polls the specified
    variables at the given interval. Results are written to a JSONL file
    and kept in a ring buffer accessible via live_memory_read. Variables
    can be edited while the session runs via live_memory_write.

    IMPORTANT: Only one live_memory session per probe at a time. Stop any
    existing session before starting a new one. Stop live_memory before
    flashing or using stm32_read_memory/stm32_write_memory.

    Args:
        variables: JSON array of variables to monitor. Elements can be:
            - String: plain symbol ("blink") or C path with dots/indices
              ("g_app.dosing.cfg.pid_kp", "tasks[2].lastRunMs"), resolved
              from the ELF. Structs (size > 4 bytes) are auto-expanded into
              individual fields with dotted names; prefer a sub-struct like
              "g_app.dosing" over a huge root ("g_app") to keep the poll fast.
            - Dict with "symbol": {"symbol": "temp", "type": "float"}
              Add "expand": false to disable struct auto-expansion.
            - Dict with "address": {"address": "0x20000304", "name": "x", "width": 32}
        elf_path: Path to .elf file for symbol resolution.
        probe: Board nickname, probe nickname, or ST-Link SN. Empty auto-selects
            the only connected board.
        interval_ms: Polling interval in milliseconds (minimum 250).
        output_path: JSONL output file path. Default: /tmp/live_memory_<id>.jsonl

    Returns:
        Session ID, output path, and resolved variable list.
    """
    interval_ms = max(interval_ms, MIN_INTERVAL_MS)

    sn, target_cfg, chipid = resolve_probe_full(probe)
    if not sn or not target_cfg:
        return "ERROR: Could not resolve probe. Use stm32_list_probes to see connected boards."

    from .realtime_debug import _debug_sessions
    dbg_conflict = [sid for sid, s in _debug_sessions.items() if s.sn == sn]
    if dbg_conflict:
        return (f"ERROR: debug session {dbg_conflict[0]} is running on this probe. "
                f"Stop it with debug_session_stop first.")

    try:
        resolved = _resolve_variables(variables, elf_path)
    except ValueError as e:
        return f"ERROR: {e}"

    if not resolved:
        return "ERROR: No variables specified."

    session_id = uuid.uuid4().hex[:8]
    if not output_path:
        output_path = f"/tmp/live_memory_{session_id}.jsonl"

    session = LiveMemorySession(
        session_id=session_id,
        variables=resolved,
        sn=sn,
        target_cfg=target_cfg,
        chipid=chipid,
        interval_ms=interval_ms,
        output_path=output_path,
        elf_path=elf_path,
    )

    loop = asyncio.get_event_loop()
    try:
        proc, sock = await loop.run_in_executor(
            _executor,
            lambda: _start_openocd(sn, target_cfg, chipid),
        )
    except (FileNotFoundError, RuntimeError) as e:
        return f"ERROR: {e}"

    session.process = proc
    session.tcl_sock = sock
    session.start_time = time.monotonic()

    thread = threading.Thread(
        target=_run_session,
        args=(session,),
        daemon=True,
        name=f"live-memory-{session_id}",
    )
    session.thread = thread
    _sessions[session_id] = session
    thread.start()

    var_lines = []
    for v in resolved:
        if v.is_float:
            type_str = "float"
        elif v.count == 2:
            type_str = "64-bit"
        else:
            type_str = f"{v.width}-bit"
        var_lines.append(f"  {v.name}: 0x{v.address:08X} ({type_str})")

    return (
        f"Live memory session started.\n"
        f"Session ID: {session_id}\n"
        f"Output: {output_path}\n"
        f"Interval: {interval_ms}ms\n"
        f"Variables:\n" + "\n".join(var_lines)
    )

async def live_memory_stop(session_id: str) -> str:
    """Stop a live memory monitoring session.

    Stops the background polling thread, kills the OpenOCD process, and
    returns session statistics.

    Args:
        session_id: Session ID from live_memory_start.

    Returns:
        Session statistics: duration, read count, error count, output path.
    """
    session = _sessions.pop(session_id, None)
    if session is None:
        available = list(_sessions.keys())
        if available:
            return f"ERROR: Session '{session_id}' not found. Active sessions: {', '.join(available)}"
        return f"ERROR: Session '{session_id}' not found. No active sessions."

    session.stop_event.set()

    if session.thread and session.thread.is_alive():
        session.thread.join(timeout=5)

    duration = time.monotonic() - session.start_time

    return (
        f"Session {session_id} stopped.\n"
        f"Duration: {duration:.1f}s\n"
        f"Reads: {session.read_count}\n"
        f"Errors: {session.error_count}\n"
        f"Output: {session.output_path}"
    )

async def live_memory_read(session_id: str, last_n: int = 10) -> str:
    """Read recent entries from a live memory session.

    Returns the last N entries from the in-memory ring buffer without
    touching the JSONL file.

    Args:
        session_id: Session ID from live_memory_start.
        last_n: Number of recent entries to return (default 10, max 100).

    Returns:
        Recent memory readings formatted as text.
    """
    session = _sessions.get(session_id)
    if session is None:
        available = list(_sessions.keys())
        if available:
            return f"ERROR: Session '{session_id}' not found. Active sessions: {', '.join(available)}"
        return f"ERROR: Session '{session_id}' not found. No active sessions."

    last_n = min(max(last_n, 1), RING_BUFFER_SIZE)

    entries = list(session.ring_buffer)[-last_n:]
    if not entries:
        alive = session.thread and session.thread.is_alive()
        status = "running" if alive else "stopped"
        return f"No data yet (session {status}, {session.read_count} reads so far)."

    lines = [f"Session {session_id} — last {len(entries)} of {session.read_count} reads:\n"]

    for entry in entries:
        elapsed = entry.get("elapsed_s", 0)
        values = entry.get("values", {})
        val_strs = []
        for name, val in values.items():
            if isinstance(val, float):
                val_strs.append(f"{name}={val:.4f}")
            elif isinstance(val, int):
                val_strs.append(f"{name}={val}")
            else:
                val_strs.append(f"{name}={val}")
        lines.append(f"  [{elapsed:7.3f}s] {', '.join(val_strs)}")

    alive = session.thread and session.thread.is_alive()
    status = "running" if alive else "stopped"
    lines.append(f"\nStatus: {status}, {session.error_count} errors")

    return "\n".join(lines)

async def live_memory_write(
    session_id: str,
    value: str,
    symbol: str = "",
    address: str = "",
    width: int = 32,
) -> str:
    """Edit a variable through a RUNNING live memory session (live expression edit).

    Writes through the session's OpenOCD connection — the monitoring session
    keeps running and the next polls show the new value. No need to stop and
    restart the session. Integers and floats are accepted ("1200", "0x4B0",
    "0.85" — floats require a 32-bit symbol).

    Args:
        session_id: Session ID from live_memory_start.
        value: Value to write — hex "0xFF", decimal "255", or float "0.85".
        symbol: Variable name or C path ("g_app.dosing.cfg.pid_kp") resolved
            from the session's ELF. Mutually exclusive with address. Width
            auto-detected from symbol size.
        address: Hex address like "0x20000304". Mutually exclusive with symbol.
        width: Access width — 8, 16, or 32 bits (address form only).

    Returns:
        Write confirmation with the target variable.
    """
    session = _sessions.get(session_id)
    if session is None:
        available = list(_sessions.keys())
        if available:
            return f"ERROR: Session '{session_id}' not found. Active sessions: {', '.join(available)}"
        return f"ERROR: Session '{session_id}' not found. No active sessions."

    if bool(symbol) == bool(address):
        return "ERROR: Provide exactly one of symbol or address."
    if not value:
        return "ERROR: value is required."

    resolved_width = max(8, min(int(width), 32))
    label = address
    if symbol:
        if not session.elf_path:
            return "ERROR: Session was started without elf_path — pass a hex address instead."
        result = resolve_symbol_flexible(session.elf_path, symbol)
        if result is None:
            return f'ERROR: Symbol "{symbol}" not found in {os.path.basename(session.elf_path)}.'
        addr, size = result
        resolved_width = _width_from_size(size)
        label = f"{symbol} (0x{addr:08X})"
    else:
        try:
            addr = int(address, 0)
        except ValueError:
            return f"ERROR: Invalid address: {address}"
        label = f"0x{addr:08X}"

    write_val, err = parse_write_value(value, resolved_width)
    if err:
        return f"ERROR: {err}"

    def _work() -> str:
        try:
            with session.tcl_lock:
                resp = _tcl_cmd(
                    session.tcl_sock,
                    f"if {{[catch {{ {_ocd_write_cmd(resolved_width)} 0x{addr:08X} 0x{write_val:X} }} e]}} "
                    f'{{ return "ERR: $e" }} else {{ return "OK" }}',
                )
        except (OSError, ConnectionError) as e:
            return f"ERROR: OpenOCD session is dead ({e}). Stop it and start a new session."
        if resp.startswith("ERR:"):
            return f"ERROR: {resp[4:].strip()}"
        fmt = {8: "02X", 16: "04X", 32: "08X"}[resolved_width]
        raw = f"0x{write_val:{fmt}}"
        shown = value.strip() if value.strip().lower() != raw else str(write_val)
        return f"Wrote {shown} to {label} (raw {raw}) — live polling continues; next reads show the new value."

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_executor, _work), timeout=10
        )
    except asyncio.TimeoutError:
        return "ERROR: Write timed out (polling thread contention?)."
