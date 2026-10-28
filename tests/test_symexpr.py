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
