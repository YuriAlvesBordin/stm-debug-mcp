
import pytest

from stm32_mcp.toolchain import get_project_name, run_openocd, validate_project_path

PROJECT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<projectDescription>
  <name>demo_firmware</name>
</projectDescription>
"""


@pytest.fixture
def project_dir(tmp_path):
    (tmp_path / ".project").write_text(PROJECT_XML)
    (tmp_path / ".cproject").write_text("<cproject/>")
    return tmp_path


def test_get_project_name_parses_xml(project_dir):
    assert get_project_name(str(project_dir)) == "demo_firmware"


def test_get_project_name_requires_project_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        get_project_name(str(tmp_path))


def test_validate_project_path_accepts_valid(project_dir):
    assert validate_project_path(str(project_dir)) == str(project_dir)


def test_validate_project_path_rejects_missing_cproject(tmp_path):
    (tmp_path / ".project").write_text(PROJECT_XML)
    with pytest.raises(FileNotFoundError):
        validate_project_path(str(tmp_path))


def test_validate_project_path_rejects_missing_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        validate_project_path(str(tmp_path / "nope"))


def test_run_openocd_rejects_malformed_serial(monkeypatch, tmp_path):
    fake = tmp_path / "openocd"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr("stm32_mcp.toolchain.find_openocd", lambda: str(fake))
    for bad in ["abc; shutdown", "sn with spaces", "sn}`", ""]:
        with pytest.raises(ValueError):
            run_openocd(bad, "stm32g0x.cfg", [])
