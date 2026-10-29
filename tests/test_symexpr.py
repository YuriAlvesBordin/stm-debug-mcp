from stm32_mcp.symexpr import is_path_expression


def test_plain_symbol_is_not_a_path():
    assert not is_path_expression("blink")
    assert not is_path_expression("SystemCoreClock")


def test_dotted_path_is_a_path():
    assert is_path_expression("g_app.dosing.cfg.pid_kp")
    assert is_path_expression("blink.state")


def test_array_index_is_a_path():
    assert is_path_expression("tasks[2].lastRunMs")
    assert is_path_expression("buf[0]")


def test_whitespace_is_ignored():
    assert is_path_expression("  g_app.x  ")
    assert not is_path_expression("  blink  ")


def test_suggest_symbols_fuzzy_match(tmp_path, monkeypatch):
    import stm32_mcp.symexpr as symexpr

    class FakeProc:
        stdout = (
            "08001550 T NvmApp_CommitTask\n"
            "08001412 T nvm_commit\n"
            "20000304 00000010 B blink\n"
            "0800043c t nvm_config_is_valid\n"
        )

    monkeypatch.setattr(symexpr, "find_nm", lambda: "nm")
    monkeypatch.setattr(
        symexpr.subprocess, "run", lambda *a, **k: FakeProc()
    )
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"x")
    sug = symexpr.suggest_symbols(str(elf), "g_nvm_cfg")
    assert "nvm_commit" in sug or "nvm_config_is_valid" in sug
    assert "blink" not in sug
