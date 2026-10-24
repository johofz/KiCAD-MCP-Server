"""
KiCad connects wires only at their end points. A pin or the end of another
wire lying in the middle of a wire is not connected ("Pin not connected",
"Unconnected wire endpoint" in ERC). These tests cover the splitting that
WireManager does after drawing wires and placing symbols, and the junctions
that sync_junctions must also write on sub-sheets (no sheet_instances block).
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Tuple

import pytest
import sexpdata

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.wire_manager import WireManager  # noqa: E402

# Sub-sheet with one two-pin symbol "R1" at (50, 50); pins at (50, 46.19)
# and (50, 53.81). No (sheet_instances), like every hierarchical child sheet.
SUB_SHEET = """(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(generator_version "9.0")
\t(uuid "bbbb2222-2222-2222-2222-bbbbbbbbbbbb")
\t(paper "A4")
\t(lib_symbols
\t\t(symbol "Test:R"
\t\t\t(pin_names (offset 0))
\t\t\t(exclude_from_sim no)
\t\t\t(in_bom yes)
\t\t\t(on_board yes)
\t\t\t(property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t\t(property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
\t\t\t(symbol "R_0_1"
\t\t\t\t(rectangle (start -1.016 -2.54) (end 1.016 2.54)
\t\t\t\t\t(stroke (width 0.254) (type default)) (fill (type none)))
\t\t\t)
\t\t\t(symbol "R_1_1"
\t\t\t\t(pin passive line (at 0 3.81 270) (length 1.27)
\t\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t\t(number "1" (effects (font (size 1.27 1.27)))))
\t\t\t\t(pin passive line (at 0 -3.81 90) (length 1.27)
\t\t\t\t\t(name "~" (effects (font (size 1.27 1.27))))
\t\t\t\t\t(number "2" (effects (font (size 1.27 1.27)))))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Test:R")
\t\t(at 50 50 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "cccc3333-3333-3333-3333-cccccccccccc")
\t\t(property "Reference" "R1" (at 52 50 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "10k" (at 54 50 0) (effects (font (size 1.27 1.27))))
\t\t(pin "1" (uuid "dddd4444-4444-4444-4444-dddddddddd01"))
\t\t(pin "2" (uuid "dddd4444-4444-4444-4444-dddddddddd02"))
\t)
)
"""


def _write(tmp_path: Path, text: str = SUB_SHEET) -> Path:
    sch = tmp_path / "child.kicad_sch"
    sch.write_text(text, encoding="utf-8")
    return sch


def _wires(sch: Path) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    data = sexpdata.loads(sch.read_text(encoding="utf-8"))
    out = []
    for item in data:
        parsed = WireManager._parse_wire(item)
        if parsed is not None:
            out.append((tuple(parsed[0]), tuple(parsed[1])))
    return out


def _junctions(sch: Path) -> List[Tuple[float, float]]:
    data = sexpdata.loads(sch.read_text(encoding="utf-8"))
    return [(x / 10000, y / 10000) for (x, y) in WireManager._get_existing_junctions(data).keys()]


def _has_endpoint(wires: Any, pt: Tuple[float, float]) -> int:
    return sum(1 for a, b in wires if a == pt or b == pt)


@pytest.mark.unit
class TestBreakAtWireEndpoints:
    def test_new_wire_through_existing_endpoint_is_split(self, tmp_path: Any) -> None:
        sch = _write(tmp_path)
        # Stub ending at (20, 30); the second wire passes straight through it.
        assert WireManager.add_wire(sch, [20.0, 20.0], [20.0, 30.0])
        assert WireManager.add_wire(sch, [10.0, 30.0], [30.0, 30.0])

        wires = _wires(sch)
        assert len(wires) == 3
        assert _has_endpoint(wires, (20.0, 30.0)) == 3

    def test_t_junction_written_on_sub_sheet(self, tmp_path: Any) -> None:
        sch = _write(tmp_path)
        assert WireManager.add_wire(sch, [20.0, 20.0], [20.0, 30.0])
        assert WireManager.add_wire(sch, [10.0, 30.0], [30.0, 30.0])

        assert (20.0, 30.0) in _junctions(sch)

    def test_polyline_through_existing_endpoint_is_split(self, tmp_path: Any) -> None:
        sch = _write(tmp_path)
        assert WireManager.add_wire(sch, [20.0, 20.0], [20.0, 30.0])
        assert WireManager.add_polyline_wire(sch, [[10.0, 30.0], [30.0, 30.0], [30.0, 40.0]])

        wires = _wires(sch)
        assert _has_endpoint(wires, (20.0, 30.0)) == 3
        assert (20.0, 30.0) in _junctions(sch)

    def test_crossing_wires_stay_unconnected(self, tmp_path: Any) -> None:
        sch = _write(tmp_path)
        assert WireManager.add_wire(sch, [20.0, 20.0], [20.0, 40.0])
        assert WireManager.add_wire(sch, [10.0, 30.0], [30.0, 30.0])

        assert len(_wires(sch)) == 2
        assert _junctions(sch) == []


@pytest.mark.unit
class TestBreakAtPins:
    def test_wire_drawn_over_pin_is_split(self, tmp_path: Any) -> None:
        sch = _write(tmp_path)
        # Horizontal wire through pin 1 of R1 at (50, 46.19).
        assert WireManager.add_wire(sch, [40.0, 46.19], [60.0, 46.19])

        wires = _wires(sch)
        assert len(wires) == 2
        assert _has_endpoint(wires, (50.0, 46.19)) == 2
        assert (50.0, 46.19) in _junctions(sch)

    def test_connect_placed_pins_splits_existing_wire(self, tmp_path: Any) -> None:
        # Wire exists before the symbol: same file, symbol already in it, wire
        # injected raw (as if drawn before the part was placed).
        text = SUB_SHEET.rstrip().rstrip(")") + (
            "\t(wire (pts (xy 50 30) (xy 50 46.19))\n"
            "\t\t(stroke (width 0) (type default))\n"
            '\t\t(uuid "eeee5555-5555-5555-5555-eeeeeeeeeeee"))\n'
            "\t(wire (pts (xy 40 53.81) (xy 60 53.81))\n"
            "\t\t(stroke (width 0) (type default))\n"
            '\t\t(uuid "eeee5555-5555-5555-5555-eeeeeeeeeeef"))\n'
            ")\n"
        )
        sch = _write(tmp_path, text)

        splits = WireManager.connect_placed_pins(sch)

        assert splits == 1
        wires = _wires(sch)
        assert len(wires) == 3
        assert _has_endpoint(wires, (50.0, 53.81)) == 2
        # Pin 1 already sat on the end of the vertical wire: untouched.
        assert _has_endpoint(wires, (50.0, 46.19)) == 1

    def test_connect_placed_pins_leaves_clean_file_alone(self, tmp_path: Any) -> None:
        sch = _write(tmp_path)
        before = sch.read_text(encoding="utf-8")

        assert WireManager.connect_placed_pins(sch) == 0
        assert sch.read_text(encoding="utf-8") == before


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")
def test_kicad_netlist_sees_pin_on_split_wire(tmp_path: Any) -> None:
    """eeschema's own connectivity must put R1.2 on the net of the split wire."""
    import re

    root_uuid = "aaaa1111-1111-1111-1111-aaaaaaaaaaaa"
    text = SUB_SHEET.replace("bbbb2222-2222-2222-2222-bbbbbbbbbbbb", root_uuid)
    text = text.replace(
        '\t\t(pin "2" (uuid "dddd4444-4444-4444-4444-dddddddddd02"))\n',
        '\t\t(pin "2" (uuid "dddd4444-4444-4444-4444-dddddddddd02"))\n'
        '\t\t(instances (project "t" (path "/' + root_uuid + '" (reference "R1") (unit 1))))\n',
    )
    text = text.rstrip()[:-1] + (
        '\t(label "N" (at 40 53.81 0) (effects (font (size 1.27 1.27)))'
        ' (uuid "ffff6666-6666-6666-6666-ffffffffffff"))\n'
        '\t(sheet_instances (path "/" (page "1")))\n)\n'
    )
    root = tmp_path / "t.kicad_sch"
    root.write_text(text, encoding="utf-8")
    # Wire over pin 2 at (50, 53.81); the label sits on its left end.
    assert WireManager.add_wire(root, [40.0, 53.81], [60.0, 53.81])

    net = tmp_path / "out.net"
    subprocess.run(
        [
            "kicad-cli",
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadxml",
            "-o",
            str(net),
            str(root),
        ],
        check=True,
        capture_output=True,
    )
    xml = net.read_text(encoding="utf-8")
    m = re.search(r'<net code="\d+" name="/N"[^>]*>(.*?)</net>', xml, re.S)
    assert m, xml
    assert 'ref="R1" pin="2"' in m.group(1)
