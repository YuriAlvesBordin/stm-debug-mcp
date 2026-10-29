"""struct_layout — use GDB ptype /o to expand struct fields from ELF DWARF info."""

import os
import re
import subprocess
from dataclasses import dataclass

from .toolchain import find_gdb

MAX_FIELDS = 64

_PRIMITIVE_TYPES = {
    "_Bool", "bool",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int8_t", "int16_t", "int32_t", "int64_t",
    "char", "signed char", "unsigned char",
    "short", "unsigned short",
    "int", "unsigned int",
    "long", "unsigned long",
    "float", "double",
}

@dataclass
class FieldInfo:
    name: str
    offset: int
    size: int
    is_float: bool = False
    type_name: str = ""
    enum_values: dict | None = None

def _run_gdb_ptype(gdb: str, elf_path: str, type_names: list[str]) -> str:
    """Run GDB batch with ptype /o for each type name. Returns combined stdout."""
    cmd = [gdb, "-batch"]
    for name in type_names:
        cmd.extend(["-ex", f"ptype /o {name}"])
    cmd.append(elf_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout

def _parse_ptype_fields(ptype_output: str) -> list[tuple[int, int, str, str]] | None:
    """Parse one ptype /o block into [(offset, size, type_name, field_name), ...].

    Returns None if the output doesn't describe a struct (i.e., it's a scalar/typedef).
    """
    if "struct" not in ptype_output and "union" not in ptype_output:
        return None

    pattern = re.compile(
        r'/\*\s*(\d+)\s*\|\s*(\d+)\s*\*/\s+'
        r'(.+?)\s+'
        r'(\w+)\s*;'
    )

    fields = []
    for m in pattern.finditer(ptype_output):
        offset = int(m.group(1))
        size = int(m.group(2))
        type_name = m.group(3).strip()
        field_name = m.group(4)
        fields.append((offset, size, type_name, field_name))

    return fields if fields else None

def _split_ptype_blocks(output: str) -> list[str]:
    """Split combined GDB output into individual ptype blocks."""
    blocks = []
    current = []
    for line in output.splitlines():
        if line.startswith("type = "):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks

def _parse_enum(ptype_output: str) -> dict | None:
    """Parse a ptype block into {value: enumerator_name} if it is an enum."""
    if "enum" not in ptype_output:
        return None
    out = {}
    for name, value in re.findall(r"(\w+)\s*=\s*(-?\d+)", ptype_output):
        out[int(value)] = name
    return out or None

def expand_struct(elf_path: str, symbol_name: str) -> list[FieldInfo] | None:
    """Use GDB ptype /o to get struct field layout. Returns None for scalars.

    For nested structs, recursively expands fields with dotted name paths.
    Enum-typed fields get a value-to-name mapping so monitors can display
    enumerator names instead of raw integers.
    """
    gdb = find_gdb()
    if not gdb:
        return None

    if not os.path.isfile(elf_path):
        return None

    output = _run_gdb_ptype(gdb, elf_path, [symbol_name])
    if not output:
        return None

    blocks = _split_ptype_blocks(output)
    if not blocks:
        return None

    top_fields = _parse_ptype_fields(blocks[0])
    if top_fields is None:
        return None

    nested_types = []
    for offset, size, type_name, field_name in top_fields:
        if type_name not in _PRIMITIVE_TYPES:
            nested_types.append(type_name)

    nested_layouts: dict[str, list[tuple[int, int, str, str]] | None] = {}
    if nested_types:
        nested_output = _run_gdb_ptype(gdb, elf_path, nested_types)
        if nested_output:
            nested_blocks = _split_ptype_blocks(nested_output)
            for type_name, block in zip(nested_types, nested_blocks):
                nested_layouts[type_name] = _parse_ptype_fields(block)

    enum_types = sorted({
        t for _, _, t, _ in top_fields
        if t not in _PRIMITIVE_TYPES and nested_layouts.get(t) is None
    })
    enum_maps: dict[str, dict] = {}
    if enum_types:
        enum_output = _run_gdb_ptype(gdb, elf_path, enum_types)
        for type_name, block in zip(enum_types, _split_ptype_blocks(enum_output)):
            mapping = _parse_enum(block)
            if mapping is not None:
                enum_maps[type_name] = mapping

    result: list[FieldInfo] = []
    for offset, size, type_name, field_name in top_fields:
        nested = nested_layouts.get(type_name)
        is_float = type_name == "float" and size <= 4
        if nested is not None:

            for n_offset, n_size, n_type, n_name in nested:
                result.append(FieldInfo(
                    name=f"{field_name}.{n_name}",
                    offset=offset + n_offset,
                    size=min(n_size, 4),
                    is_float=n_type == "float" and n_size <= 4,
                    type_name=n_type,
                    enum_values=enum_maps.get(n_type),
                ))
                if len(result) >= MAX_FIELDS:
                    break
        else:
            result.append(FieldInfo(
                name=field_name,
                offset=offset,
                size=min(size, 4),
                is_float=is_float,
                type_name=type_name,
                enum_values=enum_maps.get(type_name),
            ))

        if len(result) >= MAX_FIELDS:
            break

    return result
