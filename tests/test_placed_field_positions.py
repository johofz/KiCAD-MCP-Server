"""
Placed Reference/Value positions follow the symbol transform of eeschema.

Library field positions are Y-up like pins. eeschema places them with the
same transform as pins: Y-flip, rotation, mirror, translation. power:GND
keeps its value below the symbol, power:+3V3 above the arrow.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.dynamic_symbol_loader import DynamicSymbolLoader  # noqa: E402

LIB = (
    '\t\t(symbol "power:GND" (power)\n'
    '\t\t\t(property "Reference" "#PWR" (at 0 -6.35 0) (effects (font (size 1.27 1.27)) (hide yes)))\n'
    '\t\t\t(property "Value" "GND" (at 0 -3.81 0) (effects (font (size 1.27 1.27))))\n'
    "\t\t)\n"
    '\t\t(symbol "power:+3V3" (power)\n'
    '\t\t\t(property "Reference" "#PWR" (at 0 -3.81 0) (effects (font (size 1.27 1.27)) (hide yes)))\n'
    '\t\t\t(property "Value" "+3V3" (at 0 3.556 0) (effects (font (size 1.27 1.27))))\n'
    "\t\t)\n"
    '\t\t(symbol "Device:C"\n'
    '\t\t\t(property "Reference" "C" (at 0.635 2.54 0) (effects (font (size 1.27 1.27)) (justify left)))\n'
    '\t\t\t(property "Value" "C" (at 0.635 -2.54 0) (effects (font (size 1.27 1.27)) (justify left)))\n'
    "\t\t)\n"
)


def _sheet(tmp_path: Path) -> Path:
    sch = tmp_path / "t.kicad_sch"
    sch.write_text(
        '(kicad_sch\n\t(version 20250114)\n\t(generator "eeschema")\n\t(generator_version "9.0")\n'
        '\t(uuid "aaaaaaaa-0000-0000-0000-000000000001")\n\t(paper "A4")\n'
        f"\t(lib_symbols\n{LIB}\t)\n"
        '\t(sheet_instances (path "/" (page "1")))\n)\n',
        encoding="utf-8",
    )
    return sch


def _field(sch: Path, ref: str, name: str):
    text = sch.read_text(encoding="utf-8")
    start = text.index(f'(property "Reference" "{ref}"')
    block = text[text.rindex("(symbol", 0, start) :]
    m = re.search(r'\(property "' + name + r'" "[^"]*"\s+\(at ([-\d.]+) ([-\d.]+)', block)
    return float(m.group(1)), float(m.group(2))


def _place(sch, lib, sym, ref, angle=0, mirror_x=False, mirror_y=False):
    ok = DynamicSymbolLoader().create_component_instance(
        sch,
        lib,
        sym,
        ref,
        value=sym,
        x=50.8,
        y=50.8,
        angle=angle,
        mirror_x=mirror_x,
        mirror_y=mirror_y,
    )
    assert ok


@pytest.mark.unit
def test_gnd_value_sits_below_the_symbol(tmp_path) -> None:
    sch = _sheet(tmp_path)
    _place(sch, "power", "GND", "#PWR01")
    assert _field(sch, "#PWR01", "Value") == (50.8, 54.61)


@pytest.mark.unit
def test_supply_value_sits_above_the_arrow(tmp_path) -> None:
    sch = _sheet(tmp_path)
    _place(sch, "power", "+3V3", "#PWR02")
    assert _field(sch, "#PWR02", "Value") == (50.8, 47.244)


@pytest.mark.unit
def test_capacitor_reference_above_value(tmp_path) -> None:
    sch = _sheet(tmp_path)
    _place(sch, "Device", "C", "C1")
    assert _field(sch, "C1", "Reference") == (51.435, 48.26)
    assert _field(sch, "C1", "Value") == (51.435, 53.34)


@pytest.mark.unit
def test_rotated_capacitor_fields(tmp_path) -> None:
    # Rotation 90 maps screen (x, y) -> (y, -x): lib (0.635, 2.54) -> screen
    # (0.635, -2.54) -> (-2.54, -0.635).
    sch = _sheet(tmp_path)
    _place(sch, "Device", "C", "C2", angle=90)
    assert _field(sch, "C2", "Reference") == (48.26, 50.165)


@pytest.mark.unit
def test_mirrored_capacitor_fields(tmp_path) -> None:
    sch = _sheet(tmp_path)
    _place(sch, "Device", "C", "C3", mirror_x=True)
    assert _field(sch, "C3", "Reference") == (51.435, 53.34)
    _place(sch, "Device", "C", "C4", mirror_y=True)
    assert _field(sch, "C4", "Reference") == (50.165, 48.26)


@pytest.mark.unit
def test_placed_symbol_takes_library_bom_flags(tmp_path) -> None:
    """A test point is (in_bom no) in the library and must stay so when placed."""
    sch = tmp_path / "t.kicad_sch"
    sch.write_text(
        '(kicad_sch\n\t(version 20250114)\n\t(generator "eeschema")\n\t(generator_version "9.0")\n'
        '\t(uuid "aaaaaaaa-0000-0000-0000-000000000001")\n\t(paper "A4")\n'
        "\t(lib_symbols\n"
        '\t\t(symbol "Connector:TestPoint"\n\t\t\t(exclude_from_sim no)\n\t\t\t(in_bom no)\n'
        "\t\t\t(on_board yes)\n"
        '\t\t\t(property "Reference" "TP" (at 0 5.842 0) (effects (font (size 1.27 1.27))))\n'
        "\t\t)\n"
        '\t\t(symbol "Device:C"\n\t\t\t(in_bom yes)\n'
        '\t\t\t(property "Reference" "C" (at 0.635 2.54 0) (effects (font (size 1.27 1.27))))\n'
        "\t\t)\n"
        "\t)\n"
        '\t(sheet_instances (path "/" (page "1")))\n)\n',
        encoding="utf-8",
    )
    _place(sch, "Connector", "TestPoint", "TP1")
    _place(sch, "Device", "C", "C1")

    text = sch.read_text(encoding="utf-8")
    tp = text[text.index('(lib_id "Connector:TestPoint")') :]
    c1 = text[text.index('(lib_id "Device:C")') :]
    assert "(in_bom no)" in tp[: tp.index("(property")]
    assert "(in_bom yes)" in c1[: c1.index("(property")]
