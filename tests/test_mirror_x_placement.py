"""
Tests for add_schematic_component's mirrorX option.

Covers:
  - (mirror x) written for mirrorX, (mirror y) for mirrorY, and a 180° turn
    without any mirror token when both are set (KiCad never writes two)
  - mirrorX reaching the loader through _handle_add_schematic_component
  - Pin positions of a mirrored op-amp unit against eeschema itself: wire stubs
    and labels placed by connect_to_net at the computed pin positions must
    land on the named pins in kicad-cli's netlist, for every rotation, and the
    junction bookkeeping (WireManager._collect_pin_positions) must see the
    same pin positions (skipped where kicad-cli / KiCad's libraries are absent)
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest
import sexpdata

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils.kicad_cli import resolve_kicad_cli  # noqa: E402

TEMPLATES_DIR = Path(__file__).parent.parent / "python" / "templates"
EMPTY_SCH = TEMPLATES_DIR / "empty.kicad_sch"

_HEADER_RE = re.compile(
    r"\(symbol \(lib_id \"[^\"]+\"\) \(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)([^\n]*)"
)


def _placed_header(path: Path) -> tuple:
    """(x, y, angle, rest-of-header-line) of the first placed symbol."""
    m = _HEADER_RE.search(path.read_text(encoding="utf-8"))
    assert m, "no placed symbol header found"
    return float(m.group(1)), float(m.group(2)), float(m.group(3)), m.group(4)


def _make_iface() -> Any:
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
    return iface


# ---------------------------------------------------------------------------
# Writer: DynamicSymbolLoader.create_component_instance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMirrorTokens:
    def _place(self, tmp_path: Path, **kwargs: Any) -> Path:
        from commands.dynamic_symbol_loader import DynamicSymbolLoader

        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        DynamicSymbolLoader().create_component_instance(
            sch, "Device", "R", reference="R1", value="10k", x=50.8, y=50.8, **kwargs
        )
        return sch

    def test_mirror_x(self, tmp_path):
        _, _, angle, rest = _placed_header(self._place(tmp_path, mirror_x=True))
        assert angle == 0
        assert "(mirror x)" in rest and "(mirror y)" not in rest

    def test_mirror_y_unchanged(self, tmp_path):
        _, _, angle, rest = _placed_header(self._place(tmp_path, mirror_y=True))
        assert angle == 0
        assert "(mirror y)" in rest and "(mirror x)" not in rest

    def test_mirror_x_with_rotation(self, tmp_path):
        _, _, angle, rest = _placed_header(self._place(tmp_path, angle=90, mirror_x=True))
        assert angle == 90
        assert "(mirror x)" in rest

    @pytest.mark.parametrize("angle,expected", [(0, 180), (90, 270), (180, 0), (270, 90)])
    def test_both_mirrors_become_a_half_turn(self, tmp_path, angle, expected):
        sch = self._place(tmp_path, angle=angle, mirror_x=True, mirror_y=True)
        _, _, written, rest = _placed_header(sch)
        assert written == expected
        assert "(mirror" not in rest
        assert "(mirror" not in sch.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Handler: mirrorX inside the component dict (as src/tools/schematic.ts sends it)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerPassesMirrorX:
    def _call(self, tmp_path: Path, component: Dict[str, Any]) -> tuple:
        from commands.dynamic_symbol_loader import DynamicSymbolLoader

        sch = tmp_path / "test.kicad_sch"
        shutil.copy(EMPTY_SCH, sch)
        iface = _make_iface()
        base = {"library": "Device", "type": "R", "reference": "R1", "x": 50.8, "y": 50.8}
        with patch.object(DynamicSymbolLoader, "inject_symbol_into_schematic"):
            result = iface._handle_add_schematic_component(
                {"schematicPath": str(sch), "component": {**base, **component}}
            )
        assert result["success"] is True, result
        return _placed_header(sch)

    def test_mirror_x(self, tmp_path):
        _, _, _, rest = self._call(tmp_path, {"mirrorX": True})
        assert "(mirror x)" in rest

    def test_both(self, tmp_path):
        _, _, angle, rest = self._call(tmp_path, {"mirrorX": True, "mirrorY": True, "angle": 90})
        assert angle == 270
        assert "(mirror" not in rest

    def test_default_has_no_mirror(self, tmp_path):
        _, _, _, rest = self._call(tmp_path, {})
        assert "(mirror" not in rest


# ---------------------------------------------------------------------------
# Pin math for (mirror x): flip the placed (already rotated) symbol upside down
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_mirror_x_negates_screen_y_after_rotation(rotation):
    from commands.wire_dragger import WireDragger

    plain = WireDragger.pin_world_xy(-7.62, 2.54, 100, 100, rotation, False, False)
    mirrored = WireDragger.pin_world_xy(-7.62, 2.54, 100, 100, rotation, True, False)
    assert mirrored[0] == pytest.approx(plain[0])
    assert mirrored[1] - 100 == pytest.approx(-(plain[1] - 100))


# ---------------------------------------------------------------------------
# Against kicad-cli: a mirrored op-amp unit, wired by connect_to_net
# ---------------------------------------------------------------------------

_OPAMP = ("Amplifier_Operational", "OPA4197xPW")


def _kicad_cli_or_skip() -> str:
    cli = resolve_kicad_cli()
    if not cli:
        pytest.skip("kicad-cli not available")
    version = subprocess.run([cli, "version"], capture_output=True, text=True).stdout
    if int(re.match(r"\s*(\d+)", version or "0").group(1)) < 9:
        pytest.skip("the fixture uses the KiCad 9 file format")
    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    if DynamicSymbolLoader().find_library_file(_OPAMP[0]) is None:
        pytest.skip("KiCad's Amplifier_Operational symbol library not installed")
    return cli


def _netlist_pins(cli: str, sch: Path) -> Dict[tuple, str]:
    """{(ref, pin): net name} from kicad-cli's netlist of *sch*."""
    out = sch.with_suffix(".net")
    run = subprocess.run(
        [cli, "sch", "export", "netlist", "-o", str(out), str(sch)],
        capture_output=True,
        text=True,
    )
    assert out.exists(), run.stdout + run.stderr
    data = sexpdata.loads(out.read_text(encoding="utf-8"))
    nets = next(i for i in data[1:] if isinstance(i, list) and str(i[0]) == "nets")
    pins: Dict[tuple, str] = {}
    for net in nets[1:]:
        name = next(str(p[1]) for p in net[1:] if isinstance(p, list) and str(p[0]) == "name")
        for node in net[1:]:
            if isinstance(node, list) and str(node[0]) == "node":
                fields = {str(f[0]): str(f[1]) for f in node[1:] if isinstance(f, list)}
                pins[(fields["ref"], fields["pin"])] = name.lstrip("/")
    return pins


@pytest.mark.integration
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("mirror", ["x", "xy"])
def test_mirrored_opamp_pins_match_kicad_cli(tmp_path, rotation, mirror):
    cli = _kicad_cli_or_skip()
    from commands.connection_schematic import ConnectionManager
    from commands.wire_manager import WireManager

    (tmp_path / "mirror.kicad_pro").write_text("{}\n", encoding="utf-8")
    sch = tmp_path / "mirror.kicad_sch"
    sch.write_text(
        '(kicad_sch (version 20250114) (generator "eeschema") (generator_version "9.0")\n'
        '\t(uuid "aaaaaaaa-0000-0000-0000-00000000000a")\n\t(paper "A4")\n\t(lib_symbols)\n'
        '\t(sheet_instances (path "/" (page "1")))\n)\n',
        encoding="utf-8",
    )
    iface = _make_iface()
    result = iface._handle_add_schematic_component(
        {
            "schematicPath": str(sch),
            "component": {
                "library": _OPAMP[0],
                "type": _OPAMP[1],
                "reference": "U1",
                "x": 101.6,
                "y": 101.6,
                "unit": 1,
                "angle": rotation,
                "mirrorX": True,
                "mirrorY": mirror == "xy",
            },
        }
    )
    assert result["success"] is True, result
    header = _placed_header(sch)[3]
    assert ("(mirror x)" in header) == (mirror == "x")

    located = iface._handle_get_schematic_pin_locations(
        {"schematicPath": str(sch), "reference": "U1"}
    )
    assert located["success"] is True, located
    unit_pins = {p: located["pins"][p] for p in ("1", "2", "3")}

    # The junction bookkeeping sees the same pin positions.
    collected = {
        (round(x, 3), round(y, 3))
        for x, y in WireManager._collect_pin_positions(sexpdata.loads(sch.read_text()))
    }
    for pin, pos in unit_pins.items():
        assert (round(pos["x"], 3), round(pos["y"], 3)) in collected, (pin, pos)

    for pin in unit_pins:
        r = ConnectionManager.connect_to_net(sch, "U1", pin, f"PIN{pin}")
        assert r is True or (isinstance(r, dict) and r.get("success")), r

    nets = _netlist_pins(cli, sch)
    for pin in unit_pins:
        assert nets.get(("U1", pin)) == f"PIN{pin}", (
            f"rotation={rotation} mirror={mirror}: U1.{pin} is on {nets.get(('U1', pin))!r}, "
            f"pins at {unit_pins}"
        )
