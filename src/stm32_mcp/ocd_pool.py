"""ocd_pool — shared OpenOCD connection per probe.

Every tool family used to run its own OpenOCD: one-shot tools spawned a
process per call (~50ms+ of process startup each), while debug_* and
live_memory each held a dedicated persistent instance — and any two of
them fighting over the same ST-Link had to be stopped manually first.

The pool keeps exactly one persistent OpenOCD per probe serial and
multiplexes everything over its TCL port:

  - one-shot tools (memory read/write, flash, board info) send their
    commands through the shared connection instead of spawning processes;
  - debug sessions and live memory sessions take a *lease* on the shared
    connection (refcounted), so they coexist instead of excluding each
    other;
  - leases pin the connection (the idle reaper never kills it while a
    session holds one); an idle connection with no leases is reaped after
    OCD_POOL_IDLE_S so the probe is freed for other host tools.

Commands run serialized per connection (a lock), which is fine: OpenOCD's
TCL port serves one command at a time anyway. If the OpenOCD process dies,
the next command transparently restarts it and retries once.
"""

import socket
import subprocess
import sys
import threading
import time

from .toolchain import find_openocd

TCL_PORT = 6666
TCL_TERMINATOR = b"\x1a"
CONNECT_TIMEOUT = 5.0
OCD_POOL_IDLE_S = 300
REAP_INTERVAL_S = 30

class SharedOcd:
    """One persistent OpenOCD process + TCL socket for one probe."""

    def __init__(self, sn: str, target_cfg: str, chipid: int):
        self.sn = sn
        self.target_cfg = target_cfg
        self.chipid = chipid
        self.lock = threading.Lock()
        self.users = 0  # leases held by debug/live sessions
        self.last_used = time.monotonic()
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

_conns: dict[str, SharedOcd] = {}
_pool_lock = threading.Lock()
_reaper_started = False


def _pdeathsig():
    """Preexec hook: kill OpenOCD if this process dies (prevents an orphaned
    OpenOCD from claiming the ST-Link until manually killed)."""
    if sys.platform != "linux":
        return None

    def _set():
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, 15)

    return _set


def _spawn(conn: SharedOcd) -> None:
    """Start the OpenOCD process for a SharedOcd and connect to its TCL port."""
    openocd = find_openocd()
    if not openocd:
        raise FileNotFoundError(
            "OpenOCD not found. Install it via your package manager "
            "(e.g. 'sudo apt install openocd' or 'brew install open-ocd')."
        )

    from .toolchain import (
        _HLA_NEWTAP_FIX,
        _openocd_scripts_dir,
        openocd_workarea,
    )

    cmd = [openocd]
    scripts_dir = _openocd_scripts_dir(openocd)
    if scripts_dir:
        cmd.extend(["-s", scripts_dir])
    cmd.extend([
        "-f", "interface/stlink.cfg",
        "-c", f"adapter serial {conn.sn}",
        "-c", "transport select hla_swd",
    ])
    if scripts_dir:
        cmd.extend(["-c", _HLA_NEWTAP_FIX])
    wa = openocd_workarea(conn.chipid) if conn.chipid else None
    if wa is not None:
        cmd.extend(["-c", f"set WORKAREASIZE 0x{wa:X}"])
    cmd.extend(["-f", f"target/{conn.target_cfg}", "-c", "init"])

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            preexec_fn=_pdeathsig())

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
                    f"OpenOCD exited immediately (code {proc.returncode}):\n"
                    f"{stderr.decode(errors='replace').strip()}"
                )
            time.sleep(0.2)

    if sock is None:
        proc.terminate()
        proc.wait(timeout=5)
        raise RuntimeError(
            f"Could not connect to OpenOCD TCL port {TCL_PORT} within {CONNECT_TIMEOUT}s"
        )

    conn.proc = proc
    conn.sock = sock


def _tcl_cmd(sock: socket.socket, cmd: str) -> str:
    """Send one command to OpenOCD's TCL interface and read the response."""
    sock.sendall((cmd + "\x1a").encode())
    data = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("OpenOCD TCL connection closed")
        data.extend(chunk)
        if TCL_TERMINATOR[0] in data:
            idx = data.index(TCL_TERMINATOR[0])
            return data[:idx].decode(errors="replace")


def acquire(sn: str, target_cfg: str, chipid: int) -> SharedOcd:
    """Get (or start) the shared OpenOCD connection for a probe."""
    _start_reaper()
    with _pool_lock:
        conn = _conns.get(sn)
        if conn is None:
            conn = SharedOcd(sn, target_cfg, chipid)
            _conns[sn] = conn
        if not conn.alive():
            try:
                if conn.sock:
                    conn.sock.close()
            except OSError:
                pass
            _spawn(conn)
        conn.last_used = time.monotonic()
        return conn


def acquire_lease(sn: str, target_cfg: str, chipid: int) -> SharedOcd:
    """acquire() + a lease that pins the connection against the idle reaper.

    Used by long-lived sessions (debug_*, live_memory). Pair with
    release_lease() — never call shutdown() for a leased connection.
    """
    conn = acquire(sn, target_cfg, chipid)
    with conn.lock:
        conn.users += 1
    return conn


def release_lease(conn: SharedOcd) -> None:
    """Drop a session's lease. The connection itself stays up for other tools."""
    with conn.lock:
        conn.users = max(0, conn.users - 1)
    conn.last_used = time.monotonic()


def run_cmds(conn: SharedOcd, cmds: list, timeout_s: float = 30.0) -> str:
    """Run commands on the shared connection, returning joined TCL output.

    If the OpenOCD process died since the last use, it is restarted once and
    the commands are retried. Raises the last error if the retry also fails.
    """
    conn.last_used = time.monotonic()
    with conn.lock:
        for attempt in (1, 2):
            if not conn.alive():
                try:
                    if conn.sock:
                        conn.sock.close()
                except OSError:
                    pass
                _spawn(conn)
            try:
                outs = []
                for c in cmds:
                    outs.append(_tcl_cmd(conn.sock, c))
                conn.last_used = time.monotonic()
                return "\n".join(o for o in outs if o != "")
            except (TimeoutError, ConnectionError, OSError) as e:
                _kill(conn)
                if attempt == 2:
                    raise RuntimeError(f"OpenOCD connection failed twice: {e}") from e
        return ""


def open_socket(conn: SharedOcd) -> socket.socket:
    """Open an extra TCL client socket to the shared OpenOCD process.

    OpenOCD serves each TCL connection independently, so sessions (debug_*,
    live_memory) keep their private socket on the shared process. Serialize
    state-changing traffic with conn.lock where it must not interleave with
    pool commands (flash, one-shot reads).
    """
    if not conn.alive():
        acquire(conn.sn, conn.target_cfg, conn.chipid)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT)
    s.connect(("127.0.0.1", TCL_PORT))
    return s


def shutdown(sn: str) -> None:
    """Fully stop (and forget) the shared connection for a probe."""
    with _pool_lock:
        conn = _conns.pop(sn, None)
    if conn is not None:
        _kill(conn)


def shutdown_all() -> None:
    with _pool_lock:
        conns = list(_conns.values())
        _conns.clear()
    for conn in conns:
        _kill(conn)


def _kill(conn: SharedOcd) -> None:
    try:
        if conn.sock:
            conn.sock.close()
    except OSError:
        pass
    conn.sock = None
    if conn.proc is not None and conn.proc.poll() is None:
        try:
            conn.proc.terminate()
            conn.proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                conn.proc.kill()
            except OSError:
                pass
    conn.proc = None


def _reap_once() -> None:
    now = time.monotonic()
    with _pool_lock:
        stale = [
            sn for sn, c in _conns.items()
            if c.users == 0 and now - c.last_used > OCD_POOL_IDLE_S
        ]
    for sn in stale:
        shutdown(sn)


def _reaper() -> None:
    while True:
        time.sleep(REAP_INTERVAL_S)
        _reap_once()


def _start_reaper() -> None:
    global _reaper_started
    with _pool_lock:
        if _reaper_started:
            return
        _reaper_started = True
    t = threading.Thread(target=_reaper, daemon=True)
    t.start()
