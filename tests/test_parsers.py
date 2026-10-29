"""Unit tests for pure parsing/formatting logic in debug_tools, flash and
realtime_debug — the string heuristics that turn raw OpenOCD output into
tool results. All external I/O (OpenOCD, addr2line) is mocked.
"""


import stm32_mcp.flash as flash
import stm32_mcp.ocd_pool as ocd_pool
import stm32_mcp.realtime_debug as rd
from stm32_mcp.debug_tools import _do_read_memory, parse_write_value

# ---------------------------------------------------------------- debug_tools

def test_parse_write_value_hex_decimal():
    assert parse_write_value("0xFF", 32) == (0xFF, "")
    assert parse_write_value("255", 32) == (255, "")


def test_parse_write_value_float_ieee754():
    val, err = parse_write_value("0.25", 32)
    assert err == ""
    assert val == 0x3E800000


def test_parse_write_value_float_rejects_non_32():
    val, err = parse_write_value("1.5", 8)
    assert val is None
    assert "width 32" in err


def test_parse_write_value_invalid():
    val, err = parse_write_value("notanumber", 32)
    assert val is None
    assert "Invalid value" in err


FLASH_READ_OUTPUT = (
    "Open On-Chip Debugger\n"
    "0x20000304: deadbeef cafebabe\n"
    "0x2000030c: 0000002a\n"
)


def _fake_pool(monkeypatch, output):
    def fake_run_cmds(conn, cmds, timeout_s=30.0):
        return output
    monkeypatch.setattr(flash.ocd_pool, "run_cmds", fake_run_cmds)
    monkeypatch.setattr(
        flash.ocd_pool, "acquire",
        lambda sn, cfg, chipid: ocd_pool.SharedOcd(sn, cfg, chipid),
    )


def test_do_read_memory_parses_openocd_rows(monkeypatch):
    _fake_pool(monkeypatch, FLASH_READ_OUTPUT)
    out = _do_read_memory("0x20000304", "", "", 3, 32, "SN1", "stm32g0x.cfg", 0x460)
    assert "0xdeadbeef" in out
    assert "0xcafebabe" in out
    assert "0x0000002a" in out


def test_do_read_memory_reports_error_output(monkeypatch):
    _fake_pool(monkeypatch, "Error: target not halted")
    out = _do_read_memory("0x20000304", "", "", 1, 32, "SN1", "stm32g0x.cfg", 0x460)
    assert out.startswith("ERROR:")


# ---------------------------------------------------------------- flash

FLASH_OK_OUTPUT = (
    "stlink_serial xxx\n"
    "Writing firmware...\n"
    "** Programming Finished **\n"
    "** Verified OK **\n"
    "shutdown command invoked\n"
)


def _do_flash_with(monkeypatch, output, **kwargs):
    _fake_pool(monkeypatch, output)
    return flash._do_flash("/tmp/fw.elf", sn="SN1", target_cfg="stm32g0x.cfg", **kwargs)


def test_do_flash_success(tmp_path, monkeypatch):
    fw = tmp_path / "fw.elf"
    fw.write_bytes(b"x")
    monkeypatch.setattr(flash.os.path, "isfile", lambda p: True)
    out = _do_flash_with(monkeypatch, FLASH_OK_OUTPUT)
    assert "Flash: OK" in out
    assert "Verify: OK" in out
    assert "Reset: OK" in out


def test_do_flash_verify_failure(tmp_path, monkeypatch):
    fw = tmp_path / "fw.elf"
    fw.write_bytes(b"x")
    monkeypatch.setattr(flash.os.path, "isfile", lambda p: True)
    out = _do_flash_with(
        monkeypatch,
        "** Programming Finished **\n** Verify Failed **\n",
    )
    assert "Verify: FAILED" in out


def test_do_flash_missing_file(monkeypatch):
    out = flash._do_flash("/nope/fw.elf", sn="SN1", target_cfg="stm32g0x.cfg")
    assert out.startswith("ERROR: File not found")


def test_do_flash_read_protected(monkeypatch):
    monkeypatch.setattr(flash.os.path, "isfile", lambda p: True)
    out = _do_flash_with(monkeypatch, "Error: flash is read protected")
    assert "read-protected" in out.lower()


# ---------------------------------------------------------------- realtime_debug

def _mk_session(text_symbols=None, elf_path=""):
    conn = ocd_pool.SharedOcd("SN1", "stm32g0x.cfg", 0x460)

    class Sock:
        def settimeout(self, t):
            pass
    s = rd.DebugSession(
        session_id="t", sn="SN1", target_cfg="stm32g0x.cfg", chipid=0x460,
        elf_path=elf_path, tcl_sock=Sock(), pool_conn=conn,
    )
    s.text_symbols = text_symbols or []
    return s


def test_nearest_symbol_finds_closest_at_or_below():
    syms = [(0x08000100, 0x40, "main"), (0x08000200, 0x20, "helper")]
    assert rd._nearest_symbol(syms, 0x08000210) == ("helper", 0x10)
    assert rd._nearest_symbol(syms, 0x080001FF) == ("main", 0xFF)
    assert rd._nearest_symbol(syms, 0x080000FF) is None


def test_nearest_symbol_empty():
    assert rd._nearest_symbol([], 0x08000100) is None


def test_reg_dump_parses_openocd_reg_output(monkeypatch):
    s = _mk_session()
    reg_out = (
        "(0) pc (/32): 0x08002D91\n"
        "(14) lr (/32): 0x08001235\n"
        "(13) sp (/32): 0x20001000\n"
        "(16) xPSR (/32): 0x01000000\n"
    )
    monkeypatch.setattr(rd, "_tcl_cmd", lambda sock, cmd: reg_out)
    core = rd._core_regs(s)
    assert core["pc"] == 0x08002D91
    assert core["lr"] == 0x08001235
    assert core["sp"] == 0x20001000
    assert core["xpsr"] == 0x01000000


def test_core_regs_empty_on_bad_output(monkeypatch):
    s = _mk_session()
    monkeypatch.setattr(rd, "_tcl_cmd", lambda sock, cmd: "target halted due to error")
    assert rd._core_regs(s) == {}


def test_target_state_from_targets_listing(monkeypatch):
    s = _mk_session()
    listing = (
        "  0* stm32g0x.cpu  halted\n"
        "    1 cortex_m  -\n"
    )
    monkeypatch.setattr(rd, "_tcl_cmd", lambda sock, cmd: listing)
    assert rd._target_state(s) == "halted"


def test_format_bpwp(monkeypatch):
    assert rd._format_bpwp("", "") == "(none set)"
    out = rd._format_bpwp("Hardware breakpoint(IVA): addr=0x08002D90, len=0x2, num=0", "")
    assert "Breakpoints:" in out
    assert "0x08002D90" in out


def test_halt_report_formats_core(monkeypatch):
    s = _mk_session(text_symbols=[(0x08002D90, 0x100, "DosingApp_ControlTask")])
    reg_out = (
        "pc (/32): 0x08002D91\n"
        "lr (/32): 0x08001234\n"
        "sp (/32): 0x20001000\n"
        "xPSR (/32): 0x01000000\n"
    )
    monkeypatch.setattr(rd, "_tcl_cmd", lambda sock, cmd: reg_out)
    monkeypatch.setattr(rd, "_addr2line_map", lambda elf, addrs: {})
    out = rd._halt_report(s)
    assert "DosingApp_ControlTask" in out
    assert "0x20001000" in out


def test_sync_bpwp_takes_first_hex_per_line(monkeypatch):
    s = _mk_session(text_symbols=[(0x08002D90, 0x100, "fn")])
    monkeypatch.setattr(rd, "_addr2line_map", lambda elf, addrs: {})
    monkeypatch.setattr(
        rd, "_list_bpwp",
        lambda sess: (
            "Hardware breakpoint(IVA): addr=0x08002d91, len=0x2, num=0",
            "",
        ),
    )
    rd._sync_bpwp_lists(s)
    # Address from the line, thumb bit stripped; len/num must NOT appear.
    assert list(s.breakpoints.keys()) == [0x08002D90]
    assert s.watchpoints == {}
