"""hexfmt — annotate raw hex memory words with likely interpretations.

Memory reads return raw hex words; small integers and IEEE-754 floats are
indistinguishable at a glance. `annotate_hex` appends the float decoding for
32-bit words whose bit pattern is a plausible float (finite, sane magnitude),
so agents reading structs by address do not have to decode by hand.
"""

import math
import re

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def looks_like_float(word: int) -> float | None:
    """Return the float value if the 32-bit word plausibly IS a float.

    Heuristic: bit pattern decodes to a finite float with magnitude in a
    range typical of firmware config values. Small integers (0x000003E8 etc.)
    decode to denormals or absurdly tiny values and are left alone.
    """
    f = _as_float(word)
    if f is None or not math.isfinite(f):
        return None
    if f == 0.0:
        return None
    if 1e-3 <= abs(f) <= 1e7:
        return f
    return None


def _as_float(word: int) -> float | None:
    import struct as _struct
    try:
        return _struct.unpack("<f", _struct.pack("<I", word & 0xFFFFFFFF))[0]
    except _struct.error:
        return None


def _fmt_float(f: float) -> str:
    if abs(f) >= 1000 or f == int(f):
        return f"{f:g}"
    return f"{f:.6g}"


def annotate_hex(values: list, width: int = 32) -> str:
    """Format hex words, annotating plausible 32-bit floats with their value.

    Example: "0x3e800000(=0.25) 0x000003e8"
    """
    out = []
    for v in values:
        s = str(v)
        if not s.startswith("0x"):
            s = "0x" + s
        if width == 32 and _HEX_RE.fullmatch(s[2:]) and len(s[2:]) <= 8:
            try:
                word = int(s, 16)
            except ValueError:
                out.append(s)
                continue
            f = looks_like_float(word)
            if f is not None:
                out.append(f"{s}(={_fmt_float(f)})")
                continue
        out.append(s)
    return " ".join(out)
