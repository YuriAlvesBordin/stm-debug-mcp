"""Tests for LTO-tolerant symbol resolution.

LTO builds rename statics with optimizer suffixes ("name.3",
"fn.constprop.0.isra.0") and can drop DWARF entries entirely, leaving only
symtab addresses. Resolution must degrade gracefully instead of forcing
agents onto raw-address debugging.
"""

from stm32_mcp.debug_tools import _lto_base_name, _resolve_symbol


def test_lto_base_name_strips_suffixes():
    assert _lto_base_name("filtered_rate.3") == "filtered_rate"
    assert _lto_base_name("Scheduler_AddTask.constprop.0.isra.0") == "Scheduler_AddTask"
    assert _lto_base_name("pid_update.lto.1") == "pid_update"
    assert _lto_base_name("g_app") == "g_app"
    assert _lto_base_name("g_app.scheduler") == "g_app.scheduler"


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout


def _patch_nm(monkeypatch, stdout):
    import stm32_mcp.debug_tools as dt

    monkeypatch.setattr(dt, "find_nm", lambda: "nm")
    monkeypatch.setattr(
        dt.subprocess, "run", lambda *a, **k: _FakeProc(stdout)
    )


def test_resolve_symbol_exact_match_wins_over_renamed(tmp_path, monkeypatch):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"x")
    _patch_nm(
        monkeypatch,
        "200000c0 00000001 b s_application_active.1\n"
        "20000200 00000004 B s_application_active\n",
    )
    assert _resolve_symbol(str(elf), "s_application_active") == (0x20000200, 4)


def test_resolve_symbol_falls_back_to_lto_renamed(tmp_path, monkeypatch):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"x")
    _patch_nm(monkeypatch, "200001fc 00000004 b filtered_rate.3\n")
    assert _resolve_symbol(str(elf), "filtered_rate") == (0x200001FC, 4)


def test_resolve_symbol_falls_back_to_constprop_rename(tmp_path, monkeypatch):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"x")
    _patch_nm(
        monkeypatch,
        "08002958 00000020 t Scheduler_AddTask.constprop.0.isra.0\n",
    )
    assert _resolve_symbol(str(elf), "Scheduler_AddTask") == (0x08002958, 0x20)


def test_resolve_symbol_unknown_name_returns_none(tmp_path, monkeypatch):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"x")
    _patch_nm(monkeypatch, "200001fc 00000004 b filtered_rate.3\n")
    assert _resolve_symbol(str(elf), "totally_unknown") is None


def test_symbol_info_plain_name_uses_symtab_fallback(tmp_path, monkeypatch):
    """GDB resolves nothing (no DWARF); nm still has the address."""
    import asyncio

    import stm32_mcp.symexpr as symexpr

    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"x")

    monkeypatch.setattr(symexpr, "find_gdb", lambda: "gdb")

    def fake_run(cmd, **kwargs):
        if "gdb" in str(cmd[0]):
            out = 'SLOT s_application_active\nNo symbol "s_application_active" in current context.\n'
        else:
            out = "200000c0 00000001 b s_application_active\n"
        class P:
            stdout = out
        return P()

    monkeypatch.setattr(symexpr.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "stm32_mcp.debug_tools.find_nm", lambda: "nm"
    )
    monkeypatch.setattr(
        "stm32_mcp.debug_tools.subprocess", symexpr.subprocess
    )

    out = asyncio.run(
        symexpr.symbol_info(str(elf), "s_application_active")
    )
    assert "0x200000C0" in out
    assert "LTO" in out
    assert "NOT FOUND" not in out
