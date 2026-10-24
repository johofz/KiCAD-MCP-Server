"""
Regression tests: editing a multi-unit part edits every placed unit.

A quad op-amp placed as units 1-5 is five (symbol ...) blocks that share the
reference U1. edit_schematic_component (value / footprint / newReference /
properties) and set_/remove_schematic_component_property used to stop at the
first block, so units 2-5 kept the old value and footprint: KiCad's ERC then
reports "Units of same symbol have different values / footprints" and the
netlist and BOM take whichever unit comes first.

Covers the handler on a hand-written fixture, and on a real op-amp checked by
kicad-cli ERC (skipped where kicad-cli / KiCad's libraries are absent).
"""

import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils.kicad_cli import resolve_kicad_cli  # noqa: E402


def _make_iface() -> Any:
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
    return iface


@pytest.fixture()
def iface():
    return _make_iface()


def _unit(unit: int, x: float, reference: str = "U1", value: str = "OPA4197") -> str:
    return textwrap.dedent(
        f"""\
        \t(symbol (lib_id "Amp:Quad") (at {x} 50 0) (unit {unit})
        \t\t(uuid "00000000-0000-0000-0000-00000000010{unit}")
        \t\t(property "Reference" "{reference}" (at {x} 45 0) (effects (font (size 1.27 1.27))))
        \t\t(property "Value" "{value}" (at {x} 55 0) (effects (font (size 1.27 1.27))))
        \t\t(property "Footprint" "" (at {x} 50 0) (hide yes) (effects (font (size 1.27 1.27))))
        \t\t(instances (project "p" (path "/aaaaaaaa-0000-0000-0000-000000000001"
        \t\t\t(reference "{reference}") (unit {unit}))))
        \t)
        """
    )


def _fixture(tmp_path: Path) -> Path:
    sch = tmp_path / "p.kicad_sch"
    sch.write_text(
        '(kicad_sch (version 20250114) (generator "eeschema")\n'
        '\t(uuid "aaaaaaaa-0000-0000-0000-000000000001")\n\t(paper "A4")\n'
        '\t(lib_symbols\n\t\t(symbol "Amp:Quad" (property "Reference" "U" (at 0 0 0)))\n\t)\n'
        + _unit(1, 50)
        + _unit(2, 100)
        + _unit(3, 150)
        + _unit(1, 200, reference="U2", value="TL074")
        + '\t(sheet_instances (path "/" (page "1")))\n)\n',
        encoding="utf-8",
    )
    return sch


def _field(sch: Path, name: str) -> List[str]:
    """The *name* field of every placed symbol, in file order."""
    text = sch.read_text(encoding="utf-8")
    body = text[text.index("(symbol (lib_id") :]
    return re.findall(rf'\(property "{name}" "([^"]*)"', body)


@pytest.mark.unit
class TestEditEveryUnit:
    def test_value_and_footprint_reach_every_unit(self, iface, tmp_path):
        sch = _fixture(tmp_path)
        result = iface._handle_edit_schematic_component(
            {
                "schematicPath": str(sch),
                "reference": "U1",
                "value": "OPA4192",
                "footprint": "Package_SO:TSSOP-14_4.4x5mm_P0.65mm",
            }
        )
        assert result["success"] is True, result
        assert result["symbolsEdited"] == 3
        assert _field(sch, "Value") == ["OPA4192"] * 3 + ["TL074"]
        assert _field(sch, "Footprint") == ["Package_SO:TSSOP-14_4.4x5mm_P0.65mm"] * 3 + [""]

    def test_new_reference_renames_every_unit_and_instance(self, iface, tmp_path):
        sch = _fixture(tmp_path)
        result = iface._handle_edit_schematic_component(
            {"schematicPath": str(sch), "reference": "U1", "newReference": "U501"}
        )
        assert result["success"] is True, result
        assert _field(sch, "Reference") == ["U501"] * 3 + ["U2"]
        refs = re.findall(r'\(reference "([^"]*)"\)', sch.read_text(encoding="utf-8"))
        assert refs == ["U501"] * 3 + ["U2"]

    def test_set_property_reaches_every_unit(self, iface, tmp_path):
        sch = _fixture(tmp_path)
        result = iface._handle_set_schematic_component_property(
            {"schematicPath": str(sch), "reference": "U1", "name": "MPN", "value": "OPA4197IPWR"}
        )
        assert result["success"] is True, result
        assert result["updated"]["propertiesAdded"] == {"MPN": "OPA4197IPWR"}
        assert _field(sch, "MPN") == ["OPA4197IPWR"] * 3

    def test_new_property_is_anchored_at_each_unit(self, iface, tmp_path):
        sch = _fixture(tmp_path)
        iface._handle_set_schematic_component_property(
            {"schematicPath": str(sch), "reference": "U1", "name": "MPN", "value": "X"}
        )
        xs = re.findall(r'\(property "MPN" "X"\s+\(at ([\d.]+) ', sch.read_text())
        assert [float(x) for x in xs] == [50, 100, 150]

    def test_explicit_position_keeps_offset_per_unit(self, iface, tmp_path):
        sch = _fixture(tmp_path)
        iface._handle_set_schematic_component_property(
            {
                "schematicPath": str(sch),
                "reference": "U1",
                "name": "MPN",
                "value": "X",
                "x": 52.54,
                "y": 60,
            }
        )
        xs = re.findall(r'\(property "MPN" "X"\s+\(at ([\d.]+) ([\d.]+)', sch.read_text())
        assert [(float(x), float(y)) for x, y in xs] == [(52.54, 60), (102.54, 60), (152.54, 60)]

    def test_field_positions_keep_offset_per_unit(self, iface, tmp_path):
        sch = _fixture(tmp_path)
        iface._handle_edit_schematic_component(
            {
                "schematicPath": str(sch),
                "reference": "U1",
                "fieldPositions": {"Value": {"x": 55, "y": 58, "angle": 0}},
            }
        )
        found = re.findall(r'\(property "Value" "OPA4197" \(at ([\d.]+) ([\d.]+)', sch.read_text())
        assert [(float(x), float(y)) for x, y in found] == [(55, 58), (105, 58), (155, 58)]

    def test_remove_property_from_every_unit(self, iface, tmp_path):
        sch = _fixture(tmp_path)
        iface._handle_set_schematic_component_property(
            {"schematicPath": str(sch), "reference": "U1", "name": "MPN", "value": "X"}
        )
        result = iface._handle_remove_schematic_component_property(
            {"schematicPath": str(sch), "reference": "U1", "name": "MPN"}
        )
        assert result["success"] is True, result
        assert result["updated"]["propertiesRemoved"] == ["MPN"]
        assert _field(sch, "MPN") == []

    def test_single_unit_part_still_edited_once(self, iface, tmp_path):
        sch = _fixture(tmp_path)
        result = iface._handle_edit_schematic_component(
            {"schematicPath": str(sch), "reference": "U2", "value": "TL084"}
        )
        assert result["symbolsEdited"] == 1
        assert _field(sch, "Value") == ["OPA4197"] * 3 + ["TL084"]

    def test_unknown_reference(self, iface, tmp_path):
        sch = _fixture(tmp_path)
        result = iface._handle_edit_schematic_component(
            {"schematicPath": str(sch), "reference": "U9", "value": "x"}
        )
        assert result["success"] is False
        assert "not found" in result["message"]


@pytest.mark.integration
def test_kicad_cli_netlist_sees_one_renamed_part(tmp_path, iface):
    """Rename and re-value a five-unit op-amp: kicad-cli must list ONE part.

    With only the first unit edited, the netlist listed U501 (unit A) and U1
    (units B-E) as two parts. KiCad 9's ERC does not flag differing unit
    values, so the netlist is the oracle here.
    """
    cli = resolve_kicad_cli()
    if not cli:
        pytest.skip("kicad-cli not available")
    version = subprocess.run([cli, "version"], capture_output=True, text=True).stdout
    if int(re.match(r"\s*(\d+)", version or "0").group(1)) < 9:
        pytest.skip("the fixture uses the KiCad 9 file format")
    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    if DynamicSymbolLoader().find_library_file("Amplifier_Operational") is None:
        pytest.skip("KiCad's Amplifier_Operational symbol library not installed")

    (tmp_path / "quad.kicad_pro").write_text("{}\n", encoding="utf-8")
    sch = tmp_path / "quad.kicad_sch"
    sch.write_text(
        '(kicad_sch (version 20250114) (generator "eeschema") (generator_version "9.0")\n'
        '\t(uuid "aaaaaaaa-0000-0000-0000-00000000000b")\n\t(paper "A4")\n\t(lib_symbols)\n'
        '\t(sheet_instances (path "/" (page "1")))\n)\n',
        encoding="utf-8",
    )
    for unit in range(1, 6):
        DynamicSymbolLoader(project_path=tmp_path).add_component(
            sch,
            "Amplifier_Operational",
            "OPA4197xPW",
            reference="U1",
            value="OPA4197xPW",
            x=50.8 * unit,
            y=101.6,
            unit=unit,
        )
    result = iface._handle_edit_schematic_component(
        {
            "schematicPath": str(sch),
            "reference": "U1",
            "newReference": "U501",
            "value": "OPA4192",
            "footprint": "Package_SO:SOIC-14_3.9x8.7mm_P1.27mm",
        }
    )
    assert result["symbolsEdited"] == 5

    net = tmp_path / "quad.net"
    run = subprocess.run(
        [cli, "sch", "export", "netlist", "-o", str(net), str(sch)],
        capture_output=True,
        text=True,
    )
    assert "Failed to load schematic" not in run.stdout + run.stderr
    comps = re.findall(
        r'\(comp \(ref "([^"]+)"\)\s*\(value "([^"]*)"\)\s*\(footprint "([^"]*)"\)',
        net.read_text(encoding="utf-8"),
    )
    assert comps == [("U501", "OPA4192", "Package_SO:SOIC-14_3.9x8.7mm_P1.27mm")], comps
