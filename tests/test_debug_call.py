"""Tests for debug_call — GDB inferior function calls over the OpenOCD gdb port."""

import subprocess

import pytest

import stm32_mcp.ocd_pool as ocd_pool
import stm32_mcp.realtime_debug as rd
from stm32_mcp.realtime_debug import _do_debug_call, _validate_call_expr


def test_validate_call_expr_rejects_bad_input():
    assert _validate_call_expr("") is not None
    assert _validate_call_expr("fn(); rm -rf /") is not None  # semicolon
    assert _validate_call_expr('fn("x")') is not None  # quotes
    assert _validate_call_expr("fn(`id`)") is not None  # backtick
    assert _validate_call_expr("x" * 501) is not None


def test_validate_call_expr_accepts_normal_calls():
    assert _validate_call_expr("NvmApp_Save()") is None
    assert _validate_call_expr("pid_set_gains(&g_app.motor.pid, 1.5, 0.5, 0.08)") is None
    assert _validate_call_expr("HAL_RCC_GetHCLKFreq()") is None


class _FakeSock:
    def settimeout(self, t):
        pass


def _mk_session(elf_path="/tmp/fw.elf"):
    conn = ocd_pool.SharedOcd("SN1", "stm32g0x.cfg", 0x460)
    s = rd.DebugSession(
        session_id="t", sn="SN1", target_cfg="stm32g0x.cfg", chipid=0x460,
        elf_path=elf_path, tcl_sock=_FakeSock(), pool_conn=conn,
    )
    return s


def _patch_gdb_env(monkeypatch, gdb_stdout, state="halted", fail_find_gdb=False):
    """Patch find_gdb, the gdb subprocess and target-state helpers."""
    monkeypatch.setattr(rd, "find_gdb", lambda: None if fail_find_gdb else "gdb")

    def fake_run(cmd, capture_output, text, timeout):
        assert any("target extended-remote 127.0.0.1:3333" in a for a in cmd)
        assert any(a.startswith("print ") for a in cmd)
        assert cmd[-1] == "/tmp/fw.elf"

        class P:
            stdout = gdb_stdout
            stderr = ""
            returncode = 0
        return P()
    monkeypatch.setattr(rd.subprocess, "run", fake_run)
    monkeypatch.setattr(rd, "_target_state", lambda s: state)
    monkeypatch.setattr(rd, "_checked", lambda s, c, timeout=None: "OK")


def test_do_debug_call_returns_value(monkeypatch):
    s = _mk_session()
    _patch_gdb_env(monkeypatch, "$1 = 64000000")
    out = _do_debug_call(s, "HAL_RCC_GetHCLKFreq()")
    assert "HAL_RCC_GetHCLKFreq() -> 64000000" in out


def test_do_debug_call_halted_session_stays_halted(monkeypatch):
    s = _mk_session()
    _patch_gdb_env(monkeypatch, "$1 = 42")
    calls = []
    monkeypatch.setattr(rd, "_checked", lambda s, c, timeout=None: calls.append(c) or "OK")
    _do_debug_call(s, "NvmApp_Save()")
    # Core was already halted: no halt/resume churn.
    assert calls == []


def test_do_debug_call_resumes_running_core_afterwards(monkeypatch):
    s = _mk_session()
    _patch_gdb_env(monkeypatch, "$1 = 7")
    calls = []
    monkeypatch.setattr(rd, "_checked", lambda s, c, timeout=None: calls.append(c) or "OK")
    monkeypatch.setattr(
        rd, "_target_state",
        lambda s: "running" if not calls else "halted",
    )
    _do_debug_call(s, "fn()")
    assert "halt" in calls
    assert calls[-1] == "resume"


def test_do_debug_call_requires_elf_session(monkeypatch):
    s = _mk_session(elf_path="")
    out = _do_debug_call(s, "fn()")
    assert out.startswith("ERROR: debug_call needs symbols")


def test_do_debug_call_missing_gdb(monkeypatch):
    s = _mk_session()
    _patch_gdb_env(monkeypatch, "", fail_find_gdb=True)
    with pytest.raises(FileNotFoundError):
        _do_debug_call(s, "fn()")


def test_do_debug_call_timeout_reports_blockers(monkeypatch):
    s = _mk_session()
    monkeypatch.setattr(rd, "find_gdb", lambda: "gdb")
    monkeypatch.setattr(rd, "_target_state", lambda s: "halted")

    def fake_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(rd.subprocess, "run", fake_run)
    out = _do_debug_call(s, "HAL_Delay(1000)")
    assert "timed out" in out
    assert "HAL_Delay" in out


def test_do_debug_call_no_result_reports_gdb_tail(monkeypatch):
    s = _mk_session()
    _patch_gdb_env(
        monkeypatch,
        "No symbol \"nope\" in current context.",
    )
    out = _do_debug_call(s, "nope()")
    assert out.startswith("Call failed:")
    assert "No symbol" in out
