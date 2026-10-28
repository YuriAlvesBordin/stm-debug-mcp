"""Tests for the chip-ID maps in toolchain.py and board_map.py.

Duplicate keys in a dict literal are silently overwritten by Python, which
once caused STM32L5 (0x472) to pick the wrong OpenOCD target config. These
tests parse the module sources with ast so duplicates fail even though the
runtime dicts can no longer contain them.
"""

import ast

import stm32_mcp.board_map as board_map
import stm32_mcp.toolchain as toolchain
from stm32_mcp.toolchain import openocd_target_cfg, openocd_workarea

MODULES = [toolchain, board_map]
MAP_NAMES = ["_CHIPID_TO_TARGET", "_CHIPID_TO_UID_ADDR", "_CHIPID_TO_WORKAREA"]


def _literal_keys(module, name):
    for node in ast.walk(ast.parse(module.__file__ and open(module.__file__).read())):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return [k.value for k in node.value.keys]
    return []


def test_no_duplicate_chipid_keys_in_any_map():
    for module in MODULES:
        source = open(module.__file__).read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id in MAP_NAMES
                        and isinstance(node.value, ast.Dict)
                    ):
                        keys = [k.value for k in node.value.keys]
                        duplicates = {k for k in keys if keys.count(k) > 1}
                        assert not duplicates, (
                            f"{module.__name__}.{target.id}: duplicate keys {duplicates}"
                        )


def test_target_cfg_known_chipids():
    assert openocd_target_cfg(0x466) == "stm32g0x.cfg"
    assert openocd_target_cfg(0x472) == "stm32l5x.cfg"
    assert openocd_target_cfg(0x494) == "stm32wbx.cfg"
    assert openocd_target_cfg(0xFFFF) is None


def test_uid_addr_known_chipids():
    assert board_map._CHIPID_TO_UID_ADDR[0x472] == 0x0BFA0590
    assert board_map._CHIPID_TO_UID_ADDR[0x466] == 0x1FFF7590


def test_every_mapped_chipid_has_uid_addr():
    missing = set(toolchain._CHIPID_TO_TARGET) - set(board_map._CHIPID_TO_UID_ADDR)
    assert not missing, f"chip IDs without UID address: {sorted(hex(c) for c in missing)}"


def test_workarea_returns_int_or_none():
    assert openocd_workarea(0x494) == 0x2000
    assert openocd_workarea(0xFFFF) is None
