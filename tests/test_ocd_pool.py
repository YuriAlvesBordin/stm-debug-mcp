"""Tests for the shared per-probe OpenOCD connection pool."""

import pytest

import stm32_mcp.ocd_pool as ocd_pool


class _FakeSock:
    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        pass


class _FakeProc:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture(autouse=True)
def _clean_pool(monkeypatch):
    ocd_pool._conns.clear()
    ocd_pool._reaper_started = True  # keep the reaper thread out of unit tests
    yield
    ocd_pool._conns.clear()


def _fake_spawn(monkeypatch, responses=None, die_after=None):
    """Replace _spawn with a fake that wires a scripted TCL socket."""
    state = {"spawned": 0}

    def fake_spawn(conn):
        state["spawned"] += 1
        conn.proc = _FakeProc()
        sock = _FakeSock()
        conn.sock = sock
        if responses is not None or die_after is not None:
            import stm32_mcp.ocd_pool as p

            def scripted(_sock, cmd):
                if die_after is not None and state["spawned"] > die_after:
                    raise ConnectionError("dead")
                return responses.get(cmd, "")
            monkeypatch.setattr(p, "_tcl_cmd", scripted)
    monkeypatch.setattr(ocd_pool, "_spawn", fake_spawn)
    return state


def test_acquire_reuses_connection_per_probe(monkeypatch):
    state = _fake_spawn(monkeypatch)
    c1 = ocd_pool.acquire("SN1", "stm32g0x.cfg", 0x460)
    c2 = ocd_pool.acquire("SN1", "stm32g0x.cfg", 0x460)
    assert c1 is c2
    assert state["spawned"] == 1
    c3 = ocd_pool.acquire("SN2", "stm32g0x.cfg", 0x460)
    assert c3 is not c1
    assert state["spawned"] == 2


def test_run_cmds_joins_responses(monkeypatch):
    _fake_spawn(monkeypatch, responses={"mdw 0x20000000 1": "0x20000000: deadbeef"})
    conn = ocd_pool.acquire("SN1", "stm32g0x.cfg", 0x460)
    out = ocd_pool.run_cmds(conn, ["mdw 0x20000000 1"])
    assert "deadbeef" in out


def test_run_cmds_restarts_dead_openocd(monkeypatch):
    state = _fake_spawn(monkeypatch, responses={"poll": ""})
    conn = ocd_pool.acquire("SN1", "stm32g0x.cfg", 0x460)
    # Simulate the OpenOCD process dying between calls.
    conn.proc.returncode = 1
    out = ocd_pool.run_cmds(conn, ["poll"])
    assert state["spawned"] == 2  # restarted once
    assert out == ""


def test_lease_pins_against_reaper(monkeypatch):
    _fake_spawn(monkeypatch)
    conn = ocd_pool.acquire_lease("SN1", "stm32g0x.cfg", 0x460)
    conn.last_used -= ocd_pool.OCD_POOL_IDLE_S + 1
    ocd_pool._reap_once()
    assert "SN1" in ocd_pool._conns  # lease held: not reaped
    ocd_pool.release_lease(conn)
    conn.last_used -= ocd_pool.OCD_POOL_IDLE_S + 1
    ocd_pool._reap_once()
    assert "SN1" not in ocd_pool._conns  # idle without lease: reaped


def test_release_and_shutdown(monkeypatch):
    _fake_spawn(monkeypatch)
    conn = ocd_pool.acquire_lease("SN1", "stm32g0x.cfg", 0x460)
    ocd_pool.release_lease(conn)
    ocd_pool.shutdown("SN1")
    assert "SN1" not in ocd_pool._conns
    assert conn.proc is None


def test_run_cmds_gives_up_after_two_failures(monkeypatch):
    _fake_spawn(monkeypatch, die_after=0)
    conn = ocd_pool.acquire("SN1", "stm32g0x.cfg", 0x460)
    with pytest.raises(RuntimeError):
        ocd_pool.run_cmds(conn, ["poll"])
