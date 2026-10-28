import json

import pytest

from stm32_mcp import board_map


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(board_map, "_probe_cache", {})
    return home


def test_nickname_roundtrip(isolated_home):
    board_map.set_board_nickname("0xABC-123-456", "motor-driver")
    assert board_map.get_board_nickname("0xABC-123-456") == "motor-driver"
    saved = json.loads((isolated_home / ".stm32-mcp" / "board_map.json").read_text())
    assert saved["boards"]["0xABC-123-456"]["nickname"] == "motor-driver"


def test_probe_nickname_roundtrip(isolated_home):
    board_map.set_probe_nickname("56F7A", "yellow")
    assert board_map.get_probe_nickname("56F7A") == "yellow"
    assert board_map.get_probe_nickname("OTHER") is None


def test_unset_nickname(isolated_home):
    board_map.set_board_nickname("UID", "name")
    board_map.set_board_nickname("UID", "")
    assert board_map.get_board_nickname("UID") is None


def test_resolve_probe_passes_through_unknown_sn(isolated_home):
    assert board_map.resolve_probe("56F7A") == "56F7A"


def test_resolve_probe_by_probe_nickname(isolated_home):
    board_map.set_probe_nickname("56F7A", "yellow")
    assert board_map.resolve_probe("YELLOW") == "56F7A"


def test_resolve_probe_empty_without_boards(isolated_home):
    import stm32_mcp.board_map as bm

    def no_probes():
        return []

    bm._enumerate_probes = no_probes
    assert board_map.resolve_probe("") == ""
