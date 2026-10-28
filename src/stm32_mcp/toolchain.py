"""Toolchain discovery — find CubeIDE, OpenOCD, st-info, and ARM toolchain.

ARM TOOLCHAIN (arm-none-eabi-nm, arm-none-eabi-gdb, etc.):
  Found via PATH. Install an ARM GCC toolchain (e.g. packages
  gcc-arm-none-eabi / arm-none-eabi-gcc, or any arm-none-eabi-* on PATH).
"""

import glob
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

_cubeide_path: str | None = None
_openocd_path: str | None = None
_st_info_path: str | None = None
_stm32_programmer_cli_path: str | None = None
_wireless_binaries_dir: str | None = None
_addr2line_path: str | None = None

def find_cubeide() -> str | None:
    """Find STM32CubeIDE executable. Caches successful lookups only."""
    global _cubeide_path
    if _cubeide_path and os.path.isfile(_cubeide_path):
        return _cubeide_path

    patterns = [

        "/Applications/STM32CubeIDE.app/Contents/MacOS/stm32cubeide",

        "/opt/st/stm32cubeide_*/stm32cubeide",

        os.path.expanduser("~/st/stm32cubeide_*/stm32cubeide"),
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            _cubeide_path = sorted(matches)[-1]
            break

    return _cubeide_path

def find_openocd() -> str | None:
    """Find OpenOCD executable. Caches successful lookups only."""
    global _openocd_path
    if _openocd_path and os.path.isfile(_openocd_path):
        return _openocd_path

    on_path = shutil.which("openocd")
    if on_path:
        _openocd_path = on_path
        return _openocd_path

    candidates = [
        "/opt/homebrew/bin/openocd",
        "/usr/local/bin/openocd",
        "/usr/bin/openocd",
        os.path.expanduser("~/.local/bin/openocd"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            _openocd_path = path
            break

    if _openocd_path is None:

        globs = [
            os.path.expanduser(
                "~/st/stm32cubeide_*/plugins/"
                "com.st.stm32cube.ide.mcu.externaltools.openocd.*/"
                "tools/bin/openocd"
            ),
        ]
        for pattern in globs:
            matches = sorted(glob.glob(pattern))
            if matches:
                _openocd_path = matches[-1]
                break

    return _openocd_path

def _openocd_scripts_dir(openocd_path: str) -> str | None:
    """Find the Tcl scripts dir for an OpenOCD binary.

    Package-manager OpenOCD (brew/apt/pacman) locates its scripts itself.
    The CubeIDE-bundled binary does not — its cfgs live in a sibling plugin:
      plugins/com.st.stm32cube.ide.mcu.debug.openocd_*/resources/openocd/st_scripts
    """
    if "com.st.stm32cube.ide" not in openocd_path:
        return None

    plugins_dir = Path(openocd_path).parents[3]
    if plugins_dir.name != "plugins":
        return None

    globs = [
        str(plugins_dir / "com.st.stm32cube.ide.mcu.debug.openocd_*"
            / "resources" / "openocd" / "st_scripts"),
        str(plugins_dir / "com.st.stm32cube.ide.mcu.debug.openocd_*"
            / "resources" / "openocd" / "scripts"),
    ]
    for pattern in globs:
        matches = sorted(glob.glob(pattern))
        if matches and os.path.isfile(
            os.path.join(matches[-1], "interface", "stlink.cfg")
        ):
            return matches[-1]
    return None

_HLA_NEWTAP_FIX = (
    'proc "hla newtap" {chip tag args} { '
    "eval swd newdap $chip $tag $args "
    "}"
)

def find_stm32_programmer_cli() -> str | None:
    """Find STM32_Programmer_CLI executable. Caches successful lookups only.

    Used by the FUS / BLE stack tools to drive `-fwupgrade` on STM32WB boards.
    """
    global _stm32_programmer_cli_path
    if _stm32_programmer_cli_path and os.path.isfile(_stm32_programmer_cli_path):
        return _stm32_programmer_cli_path

    patterns = [
        "/Applications/STMicroelectronics/STM32Cube/STM32CubeProgrammer/"
        "STM32CubeProgrammer.app/Contents/Resources/bin/STM32_Programmer_CLI",
        "/opt/st/stm32cubeprog_*/bin/STM32_Programmer_CLI",
        "/opt/stm32cubeprogrammer/bin/STM32_Programmer_CLI",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            _stm32_programmer_cli_path = sorted(matches)[-1]
            break

    if _stm32_programmer_cli_path is None:
        on_path = shutil.which("STM32_Programmer_CLI")
        if on_path:
            _stm32_programmer_cli_path = on_path

    return _stm32_programmer_cli_path

def find_wireless_binaries_dir() -> str | None:
    """Find the STM32WB_Copro_Wireless_Binaries folder. Caches successful lookups.

    Resolution order:
      1. $STM32_WIRELESS_BINARIES env var
      2. ~/stm32cube-wb/Projects/STM32WB_Copro_Wireless_Binaries (FUS_INSTALL.md default)
    """
    global _wireless_binaries_dir

    if _wireless_binaries_dir and os.path.isdir(_wireless_binaries_dir):
        return _wireless_binaries_dir

    env = os.environ.get("STM32_WIRELESS_BINARIES")
    if env and os.path.isdir(env):
        _wireless_binaries_dir = env
    else:
        default = os.path.expanduser(
            "~/stm32cube-wb/Projects/STM32WB_Copro_Wireless_Binaries"
        )
        if os.path.isdir(default):
            _wireless_binaries_dir = default

    return _wireless_binaries_dir

def find_st_info() -> str | None:
    """Find st-info (open-source stlink tools). Caches successful lookups only."""
    global _st_info_path
    if _st_info_path and os.path.isfile(_st_info_path):
        return _st_info_path

    on_path = shutil.which("st-info")
    if on_path:
        _st_info_path = on_path
        return _st_info_path

    candidates = [
        "/opt/homebrew/bin/st-info",
        "/usr/local/bin/st-info",
        "/usr/bin/st-info",
        os.path.expanduser("~/.local/bin/st-info"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            _st_info_path = path
            break

    return _st_info_path

def find_nm() -> str | None:
    """Find arm-none-eabi-nm on PATH (from CubeIDE toolchain)."""
    return shutil.which("arm-none-eabi-nm")

def find_gdb() -> str | None:
    """Find arm-none-eabi-gdb on PATH (from CubeIDE toolchain)."""
    return shutil.which("arm-none-eabi-gdb")

def find_addr2line() -> str | None:
    """Find arm-none-eabi-addr2line.

    Checks PATH first, then the CubeIDE-bundled toolchain directory (same
    place nm/gdb come from). Caches successful lookups only.
    """
    global _addr2line_path
    if _addr2line_path and os.path.isfile(_addr2line_path):
        return _addr2line_path

    on_path = shutil.which("arm-none-eabi-addr2line")
    if on_path:
        _addr2line_path = on_path
        return _addr2line_path

    globs = [
        os.path.expanduser(
            "~/st/stm32cubeide_*/plugins/"
            "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*/"
            "tools/bin/arm-none-eabi-addr2line"
        ),
        "/Applications/STM32CubeIDE.app/Contents/Eclipse/plugins/"
        "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*/"
        "tools/bin/arm-none-eabi-addr2line",
    ]
    for pattern in globs:
        matches = sorted(glob.glob(pattern))
        if matches:
            _addr2line_path = matches[-1]
            break

    return _addr2line_path

_CHIPID_TO_TARGET: dict[int, str] = {

    0x440: "stm32f0x.cfg", 0x442: "stm32f0x.cfg", 0x444: "stm32f0x.cfg",
    0x445: "stm32f0x.cfg", 0x448: "stm32f0x.cfg",

    0x410: "stm32f1x.cfg", 0x412: "stm32f1x.cfg", 0x414: "stm32f1x.cfg",
    0x418: "stm32f1x.cfg", 0x420: "stm32f1x.cfg", 0x428: "stm32f1x.cfg",
    0x430: "stm32f1x.cfg",

    0x411: "stm32f2x.cfg",

    0x422: "stm32f3x.cfg", 0x432: "stm32f3x.cfg", 0x438: "stm32f3x.cfg",
    0x439: "stm32f3x.cfg", 0x446: "stm32f3x.cfg",

    0x413: "stm32f4x.cfg", 0x419: "stm32f4x.cfg", 0x421: "stm32f4x.cfg",
    0x423: "stm32f4x.cfg", 0x431: "stm32f4x.cfg", 0x433: "stm32f4x.cfg",
    0x434: "stm32f4x.cfg", 0x441: "stm32f4x.cfg", 0x458: "stm32f4x.cfg",

    0x449: "stm32f7x.cfg", 0x451: "stm32f7x.cfg", 0x452: "stm32f7x.cfg",

    0x456: "stm32g0x.cfg", 0x460: "stm32g0x.cfg", 0x466: "stm32g0x.cfg",
    0x467: "stm32g0x.cfg",

    0x468: "stm32g4x.cfg", 0x469: "stm32g4x.cfg", 0x479: "stm32g4x.cfg",

    0x450: "stm32h7x.cfg", 0x480: "stm32h7x.cfg", 0x483: "stm32h7x.cfg",

    0x425: "stm32l0.cfg", 0x417: "stm32l0.cfg", 0x447: "stm32l0.cfg",
    0x457: "stm32l0.cfg",

    0x416: "stm32l1.cfg", 0x427: "stm32l1.cfg", 0x429: "stm32l1.cfg",
    0x436: "stm32l1.cfg", 0x437: "stm32l1.cfg",

    0x415: "stm32l4x.cfg", 0x435: "stm32l4x.cfg", 0x461: "stm32l4x.cfg",
    0x462: "stm32l4x.cfg", 0x464: "stm32l4x.cfg", 0x470: "stm32l4x.cfg",
    0x471: "stm32l4x.cfg",

    0x472: "stm32l5x.cfg",

    0x494: "stm32wbx.cfg", 0x495: "stm32wbx.cfg", 0x496: "stm32wbx.cfg",

    0x497: "stm32wlx.cfg",

    0x443: "stm32c0x.cfg",

    0x455: "stm32u5x.cfg", 0x476: "stm32u5x.cfg", 0x481: "stm32u5x.cfg",
    0x482: "stm32u5x.cfg",

    0x474: "stm32h5x.cfg", 0x478: "stm32h5x.cfg", 0x484: "stm32h5x.cfg",
}

def openocd_target_cfg(chipid: int) -> str | None:
    """Map st-info chipid to OpenOCD target config filename."""
    return _CHIPID_TO_TARGET.get(chipid)

_CHIPID_TO_WORKAREA: dict[int, int] = {
    0x494: 0x2000,
    0x495: 0x2000,
    0x456: 0x2000,
    0x460: 0x2000,
    0x466: 0x2000,
    0x467: 0x2000,
}

def openocd_workarea(chipid: int) -> int | None:
    """Return override work area size for chips that need it, or None."""
    return _CHIPID_TO_WORKAREA.get(chipid)

def run_openocd(
    sn: str,
    target_cfg: str,
    commands: list[str],
    timeout: int = 15,
    chipid: int = 0,
) -> str:
    """Run OpenOCD one-shot with the given commands. Returns combined output.

    Builds a command like:
        openocd -f interface/stlink.cfg -c "adapter serial <sn>"
                -c "transport select hla_swd"
                [-c "set WORKAREASIZE <size>"]
                -f target/<target_cfg>
                -c "<cmd1>" -c "<cmd2>" ... -c "shutdown"
    """
    openocd = find_openocd()
    if not openocd:
        raise FileNotFoundError(
            "OpenOCD not found. Install it via your package manager "
            "(e.g. 'brew install open-ocd' or 'sudo apt install openocd')."
        )

    if not re.fullmatch(r"[0-9A-Fa-f]+", sn or ""):
        raise ValueError(f"Invalid ST-Link serial number: {sn!r}")

    cmd = [openocd]

    scripts_dir = _openocd_scripts_dir(openocd)
    if scripts_dir:
        cmd.extend(["-s", scripts_dir])

    cmd.extend([
        "-f", "interface/stlink.cfg",
        "-c", f"adapter serial {sn}",
        "-c", "transport select hla_swd",
    ])

    if scripts_dir:
        cmd.extend(["-c", _HLA_NEWTAP_FIX])

    wa = openocd_workarea(chipid) if chipid else None
    if wa is not None:
        cmd.extend(["-c", f"set WORKAREASIZE 0x{wa:X}"])

    cmd.extend(["-f", f"target/{target_cfg}"])
    for c in commands:
        cmd.extend(["-c", c])
    cmd.extend(["-c", "shutdown"])

    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout + "\n" + result.stderr

def get_project_name(project_path: str) -> str:
    """Parse .project XML to extract the Eclipse project name."""
    project_file = os.path.join(project_path, ".project")
    if not os.path.isfile(project_file):
        raise FileNotFoundError(f"No .project file found at {project_path}")

    tree = ET.parse(project_file)
    name_elem = tree.find("name")
    if name_elem is None or not name_elem.text:
        raise ValueError(f"Could not find <name> element in {project_file}")
    return name_elem.text

def validate_project_path(project_path: str) -> str:
    """Check that .project and .cproject exist. Returns resolved absolute path."""
    resolved = str(Path(project_path).resolve())

    if not os.path.isdir(resolved):
        raise FileNotFoundError(f"Directory does not exist: {resolved}")

    project_file = os.path.join(resolved, ".project")
    cproject_file = os.path.join(resolved, ".cproject")

    if not os.path.isfile(project_file):
        raise FileNotFoundError(
            f"No .project found at {resolved}. Is this a CubeIDE project?"
        )
    if not os.path.isfile(cproject_file):
        raise FileNotFoundError(
            f"No .cproject found at {resolved}. Is this a CubeIDE project?"
        )

    return resolved
