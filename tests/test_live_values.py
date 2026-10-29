from stm32_mcp.live_memory import ResolvedVariable, _finalize_value


def var(**kwargs):
    defaults = dict(name="x", address=0x20000000, width=32)
    defaults.update(kwargs)
    return ResolvedVariable(**defaults)


def test_float_decoded_to_decimal():
    import struct

    raw = struct.unpack("<I", struct.pack("<f", 0.85))[0]
    assert _finalize_value(var(is_float=True), raw) == 0.85


def test_signed_int_sign_extended():
    v = var(type_name="int8_t", width=8)
    assert _finalize_value(v, 0xFF) == -1
    v16 = var(type_name="int16_t", width=16)
    assert _finalize_value(v16, 0x8000) == -32768
    v32 = var(type_name="int", width=32)
    assert _finalize_value(v32, 0xFFFFFFFF) == -1
    assert _finalize_value(v32, 42) == 42


def test_unsigned_not_sign_extended():
    v = var(type_name="uint8_t", width=8)
    assert _finalize_value(v, 0xFF) == 255


def test_bool_rendered_true_false():
    assert _finalize_value(var(type_name="_Bool"), 1) == "true"
    assert _finalize_value(var(type_name="bool"), 0) == "false"


def test_enum_rendered_by_name():
    v = var(enum_values={0: "IDLE", 1: "RUN", 2: "FAULT"})
    assert _finalize_value(v, 1) == "RUN"
    assert _finalize_value(v, 99) == 99


def test_no_type_info_returns_raw():
    assert _finalize_value(var(), 0xFFFFFFFF) == 0xFFFFFFFF
