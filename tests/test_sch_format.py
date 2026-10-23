"""
New schematics take the file format of the installed KiCad.

KiCad 9 refuses a ``(version 20260101)`` schematic with "Failed to load
schematic", so a server running against KiCad 9 must write 20250114.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from utils import sch_format  # noqa: E402
from utils.sch_format import (  # noqa: E402
    KICAD9_SCH_VERSION,
    KICAD10_SCH_VERSION,
    apply_schematic_header,
    schematic_header,
    target_kicad_major,
)

_TEMPLATE = Path(__file__).resolve().parents[1] / "python" / "templates" / "blank.kicad_sch"


@pytest.fixture
def no_override(monkeypatch):
    monkeypatch.delenv("KICAD_SCH_FORMAT", raising=False)


def _pcbnew_with_version(version):
    mod = MagicMock(name="pcbnew")
    mod.Version.return_value = version
    return mod


@pytest.mark.parametrize(
    "version,expected",
    [("9.0.9", 9), ("9.0.0-rc1", 9), ("8.0.4", 9), ("10.0.1", 10), ("11.0.0", 10)],
)
def test_major_follows_pcbnew(no_override, version, expected):
    with patch.dict(sys.modules, {"pcbnew": _pcbnew_with_version(version)}):
        assert target_kicad_major() == expected


def test_unknown_pcbnew_version_defaults_to_kicad10(no_override):
    with patch.dict(sys.modules, {"pcbnew": MagicMock(name="pcbnew")}):
        assert target_kicad_major() == 10


@pytest.mark.parametrize("value,expected", [("9", 9), ("10", 10)])
def test_env_override_wins_over_pcbnew(monkeypatch, value, expected):
    monkeypatch.setenv("KICAD_SCH_FORMAT", value)
    other = "10.0.1" if expected == 9 else "9.0.9"
    with patch.dict(sys.modules, {"pcbnew": _pcbnew_with_version(other)}):
        assert target_kicad_major() == expected


def test_invalid_env_override_is_ignored(monkeypatch):
    monkeypatch.setenv("KICAD_SCH_FORMAT", "nine")
    with patch.dict(sys.modules, {"pcbnew": _pcbnew_with_version("9.0.9")}):
        assert target_kicad_major() == 9


def test_headers(monkeypatch):
    monkeypatch.setenv("KICAD_SCH_FORMAT", "9")
    assert schematic_header() == (
        f'(kicad_sch (version {KICAD9_SCH_VERSION}) (generator "eeschema")'
        ' (generator_version "9.0")'
    )
    monkeypatch.setenv("KICAD_SCH_FORMAT", "10")
    assert schematic_header() == (
        f'(kicad_sch (version {KICAD10_SCH_VERSION}) (generator "eeschema")'
        ' (generator_version "10.0")'
    )


@pytest.mark.parametrize("value,version", [("9", KICAD9_SCH_VERSION), ("10", KICAD10_SCH_VERSION)])
def test_apply_header_to_blank_template(monkeypatch, value, version):
    monkeypatch.setenv("KICAD_SCH_FORMAT", value)
    content = _TEMPLATE.read_text(encoding="utf-8")
    out = apply_schematic_header(content)
    assert out.startswith(f"(kicad_sch (version {version}) ")
    assert out.count("(kicad_sch") == 1
    # Only the header changed.
    assert out.split("\n", 1)[1] == content.split("\n", 1)[1]


def test_apply_header_without_generator_version(monkeypatch):
    monkeypatch.setenv("KICAD_SCH_FORMAT", "9")
    out = apply_schematic_header('(kicad_sch (version 20231120) (generator "x")\n  (paper "A4")\n)')
    assert out == schematic_header() + '\n  (paper "A4")\n)'


def _load_schematic_module():
    spec = importlib.util.spec_from_file_location(
        "schematic_module_sch_format",
        os.path.join(os.path.dirname(__file__), "..", "python", "commands", "schematic.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("value,version", [("9", KICAD9_SCH_VERSION), ("10", KICAD10_SCH_VERSION)])
def test_create_schematic_writes_target_format(tmp_path, monkeypatch, value, version):
    monkeypatch.setenv("KICAD_SCH_FORMAT", value)
    mod = _load_schematic_module()
    with patch.object(mod, "Schematic"):
        mod.SchematicManager.create_schematic("demo", path=str(tmp_path))
    first = (tmp_path / "demo.kicad_sch").read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith(f"(kicad_sch (version {version}) ")


def test_module_reads_pcbnew_lazily():
    # Importing the helper must not require KiCad; detection happens per call.
    assert not hasattr(sch_format, "pcbnew")
