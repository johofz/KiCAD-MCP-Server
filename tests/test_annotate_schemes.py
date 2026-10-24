"""
Tests for annotate_schematic's scheme ("sequential" | "sheet_x100") and reset.

Covers utils.symbol_instances.annotate_hierarchy:
  - sheet_x100: page P numbers each prefix P*100+1, P*100+2, ... (power and
    flag symbols too), ordered top-to-bottom then left-to-right
  - reset false keeps and avoids existing numbers; reset true renumbers all
  - a part's units sharing one reference stay one part, numbered on the
    lowest page holding one of them -- also one symbol set to units A-D in
    four uses of its sheet; same reference + same unit in two uses = two parts
  - each use of a reused sheet gets its own references
  - sequential + reset numbers the project 1, 2, 3 in page order
  - the default call is unchanged, and handler validation
  - kicad-cli netlist after sheet_x100 (skipped where kicad-cli is unavailable)
"""

import re
import subprocess
import sys
import uuid as uuidlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
import sexpdata

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.schematic_hierarchy import SchematicHierarchyCommands  # noqa: E402
from utils.kicad_cli import resolve_kicad_cli  # noqa: E402
from utils.sheet_tree import _sheet_blocks, sheet_instances  # noqa: E402
from utils.symbol_instances import _placed_symbols, _read_part, annotate_hierarchy  # noqa: E402

ROOT = "aaaa0000-0000-0000-0000-000000000001"
CHAN = "bbbb0000-0000-0000-0000-000000000002"

_LIBS = (
    "\t(lib_symbols\n"
    '\t\t(symbol "power:GND" (power) (property "Reference" "#PWR" (at 0 0 0)))\n'
    '\t\t(symbol "Device:R" (property "Reference" "R" (at 0 0 0)))\n'
    "\t)\n"
)


def _sch(own_uuid: str, *items: str, root: bool = False) -> str:
    tail = '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n' if root else ""
    return (
        '(kicad_sch\n\t(version 20250114)\n\t(generator "eeschema")\n'
        f'\t(uuid "{own_uuid}")\n\t(paper "A4")\n{_LIBS}' + "".join(items) + tail + ")\n"
    )


def _sym(
    reference: str,
    x: float,
    y: float,
    paths: List[str],
    lib_id: str = "Device:R",
    unit: int = 1,
    refs: Optional[List[str]] = None,
    units: Optional[List[int]] = None,
    symbol_uuid: Optional[str] = None,
) -> str:
    refs = refs or [reference] * len(paths)
    units = units or [unit] * len(paths)
    entries = "".join(
        f'\t\t\t\t(path "{p}"\n\t\t\t\t\t(reference "{r}")\n\t\t\t\t\t(unit {u})\n\t\t\t\t)\n'
        for p, r, u in zip(paths, refs, units)
    )
    return (
        f'\t(symbol\n\t\t(lib_id "{lib_id}")\n\t\t(at {x} {y} 0)\n\t\t(unit {unit})\n'
        f'\t\t(uuid "{symbol_uuid or uuidlib.uuid4()}")\n'
        f'\t\t(property "Reference" "{reference}"\n\t\t\t(at {x} {y} 0)\n\t\t)\n'
        f'\t\t(property "Value" "10k"\n\t\t\t(at {x} {y} 0)\n\t\t)\n'
        f'\t\t(instances\n\t\t\t(project "design"\n{entries}\t\t\t)\n\t\t)\n'
        "\t)\n"
    )


def _hierarchy(tmp_path: Path, pages: List[int]) -> Dict[str, Any]:
    """design.kicad_sch (page 1) using chan.kicad_sch once per *pages* entry."""
    (tmp_path / "design.kicad_pro").write_text("{}", encoding="utf-8")
    root = tmp_path / "design.kicad_sch"
    root.write_text(_sch(ROOT, root=True), encoding="utf-8")
    chan = tmp_path / "chan.kicad_sch"
    chan.write_text(_sch(CHAN), encoding="utf-8")
    hier = SchematicHierarchyCommands(SimpleNamespace())
    for i in range(len(pages)):
        r = hier.add_hierarchical_sheet(
            {
                "schematicPath": str(root),
                "subsheetPath": str(chan),
                "sheetName": f"ch{i + 1}",
                "position": {"x": 30 + 60 * i, "y": 50},
            }
        )
        assert r["success"], r
    # Renumber the pages as asked (add_hierarchical_sheet hands out 2, 3, ...).
    text = root.read_text(encoding="utf-8")
    for i, page in enumerate(pages):
        text = text.replace(f'(page "{i + 2}")', f'(page "#{page}")', 1)
    root.write_text(text.replace('(page "#', '(page "'), encoding="utf-8")
    uses = [u.path for u in sheet_instances(root)]
    return {"root": root, "chan": chan, "root_path": uses[0], "uses": uses[1:]}


def _write(path: Path, own_uuid: str, *items: str, root: bool = False) -> None:
    text = path.read_text(encoding="utf-8")
    # Keep the file's (sheet ...) blocks; replace everything else.
    blocks = ["\t" + block + "\n" for _u, _n, _f, block in _sheet_blocks(text)]
    path.write_text(_sch(own_uuid, *(blocks + list(items)), root=root), encoding="utf-8")


def _refs(path: Path) -> List[List[tuple]]:
    """Per placed symbol: [(path, reference, unit), ...] plus the field."""
    text = path.read_text(encoding="utf-8")
    out = []
    for start, end in _placed_symbols(text):
        part = _read_part(text, start, end, "design")
        out.append((part.field, [(e.path, e.reference, e.unit) for e in part.entries]))
    return out


def _by_use(mapping: List[Dict[str, Any]]) -> Dict[tuple, str]:
    return {(m["instancePath"], m["oldReference"], m["uuid"]): m["newReference"] for m in mapping}


@pytest.mark.unit
class TestSheetX100:
    def test_pages_and_reused_sheet(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[5, 10])
        rp, uses = h["root_path"], h["uses"]
        _write(
            h["root"],
            ROOT,
            _sym("R?", 50, 50, [rp]),
            _sym("C?", 60, 50, [rp], lib_id="Device:C"),
            root=True,
        )
        _write(
            h["chan"],
            CHAN,
            _sym("R?", 50, 50, uses),
            _sym("R?", 50, 60, uses),
            _sym("#PWR?", 50, 80, uses, lib_id="power:GND"),
        )
        mapping = annotate_hierarchy(h["root"], scheme="sheet_x100")
        news = sorted(m["newReference"] for m in mapping)
        assert news == sorted(
            ["R101", "C101", "R501", "R502", "#PWR501", "R1001", "R1002", "#PWR1001"]
        )
        chan = _refs(h["chan"])
        assert chan[0] == ("R501", [(uses[0], "R501", "1"), (uses[1], "R1001", "1")])
        assert chan[1][1] == [(uses[0], "R502", "1"), (uses[1], "R1002", "1")]
        assert chan[2] == ("#PWR501", [(uses[0], "#PWR501", "1"), (uses[1], "#PWR1001", "1")])
        assert [f for f, _ in _refs(h["root"])] == ["R101", "C101"]
        for m in mapping:
            assert set(m) == {
                "instancePath",
                "uuid",
                "unit",
                "oldReference",
                "newReference",
                "file",
            }

    def test_order_top_to_bottom_then_left_to_right(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[])
        rp = h["root_path"]
        _write(
            h["root"],
            ROOT,
            _sym("R?", 100, 50, [rp]),
            _sym("R?", 50, 100, [rp]),
            _sym("R?", 10, 50, [rp]),
            root=True,
        )
        annotate_hierarchy(h["root"], scheme="sheet_x100")
        assert [f for f, _ in _refs(h["root"])] == ["R102", "R103", "R101"]

    def test_existing_numbers_are_kept_and_avoided(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[])
        rp = h["root_path"]
        _write(
            h["root"],
            ROOT,
            _sym("R101", 90, 90, [rp]),
            _sym("R7", 80, 80, [rp]),
            _sym("R?", 10, 10, [rp]),
            root=True,
        )
        mapping = annotate_hierarchy(h["root"], scheme="sheet_x100")
        assert [(m["oldReference"], m["newReference"]) for m in mapping] == [("R?", "R102")]
        assert [f for f, _ in _refs(h["root"])] == ["R101", "R7", "R102"]

    def test_reset_renumbers_everything(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[])
        rp = h["root_path"]
        _write(
            h["root"],
            ROOT,
            _sym("R101", 90, 90, [rp]),
            _sym("R7", 80, 80, [rp]),
            _sym("R?", 10, 10, [rp]),
            root=True,
        )
        mapping = annotate_hierarchy(h["root"], scheme="sheet_x100", reset=True)
        assert len(mapping) == 3
        assert [f for f, _ in _refs(h["root"])] == ["R103", "R102", "R101"]

    def test_units_sharing_a_reference_stay_one_part(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[3])
        rp, uses = h["root_path"], h["uses"]
        # U4 units 1 and 2 on page 3 (the sub-sheet), unit 3 on page 1.
        _write(h["root"], ROOT, _sym("U4", 50, 50, [rp], lib_id="Amp:Quad", unit=3), root=True)
        _write(
            h["chan"],
            CHAN,
            _sym("U4", 50, 50, uses, lib_id="Amp:Quad", unit=1),
            _sym("U4", 90, 50, uses, lib_id="Amp:Quad", unit=2),
        )
        annotate_hierarchy(h["root"], scheme="sheet_x100", reset=True)
        assert [f for f, _ in _refs(h["root"])] == ["U101"]
        assert [f for f, _ in _refs(h["chan"])] == ["U101", "U101"]

    def test_one_symbol_four_units_across_four_uses(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[5, 6, 7, 8])
        uses = h["uses"]
        sym = "11110000-0000-0000-0000-000000000001"
        _write(
            h["chan"],
            CHAN,
            _sym(
                "U9",
                50,
                50,
                uses,
                lib_id="Amp:Quad",
                units=[1, 2, 3, 4],
                symbol_uuid=sym,
            ),
            _sym("R?", 50, 70, uses),
        )
        mapping = annotate_hierarchy(h["root"], scheme="sheet_x100", reset=True)
        opamp = [m for m in mapping if m["uuid"] == sym]
        assert {m["newReference"] for m in opamp} == {"U501"}
        assert sorted(m["unit"] for m in opamp) == [1, 2, 3, 4]
        resistors = sorted(m["newReference"] for m in mapping if m["uuid"] != sym)
        assert resistors == ["R501", "R601", "R701", "R801"]

    def test_same_reference_same_unit_in_two_uses_is_two_parts(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[2, 3])
        uses = h["uses"]
        _write(h["chan"], CHAN, _sym("R1", 50, 50, uses))  # R1 in both uses: wrong
        annotate_hierarchy(h["root"], scheme="sheet_x100", reset=True)
        assert _refs(h["chan"])[0][1] == [(uses[0], "R201", "1"), (uses[1], "R301", "1")]

    def test_unannotated_units_are_packed(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[])
        rp = h["root_path"]
        _write(
            h["root"],
            ROOT,
            _sym("U?", 10, 10, [rp], lib_id="Amp:Dual", unit=1),
            _sym("U?", 30, 10, [rp], lib_id="Amp:Dual", unit=2),
            _sym("U?", 50, 10, [rp], lib_id="Amp:Dual", unit=1),
            root=True,
        )
        annotate_hierarchy(h["root"], scheme="sheet_x100")
        assert [f for f, _ in _refs(h["root"])] == ["U101", "U101", "U102"]

    def test_annotating_from_a_sub_sheet_covers_the_project(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[4])
        rp, uses = h["root_path"], h["uses"]
        _write(h["root"], ROOT, _sym("R?", 50, 50, [rp]), root=True)
        _write(h["chan"], CHAN, _sym("R?", 50, 50, uses))
        annotate_hierarchy(h["chan"], scheme="sheet_x100")
        assert [f for f, _ in _refs(h["root"])] == ["R101"]
        assert [f for f, _ in _refs(h["chan"])] == ["R401"]

    def test_non_numeric_page(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[2])
        text = h["root"].read_text(encoding="utf-8").replace('(page "2")', '(page "A")')
        h["root"].write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="numeric page"):
            annotate_hierarchy(h["root"], scheme="sheet_x100")


@pytest.mark.unit
class TestSequentialReset:
    def test_numbers_in_page_order(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[2, 3])
        rp, uses = h["root_path"], h["uses"]
        _write(
            h["root"],
            ROOT,
            _sym("R40", 50, 50, [rp]),
            _sym("#PWR07", 50, 90, [rp], lib_id="power:GND"),
            root=True,
        )
        _write(h["chan"], CHAN, _sym("R?", 50, 50, uses), _sym("R12", 50, 10, uses))
        annotate_hierarchy(h["root"], scheme="sequential", reset=True)
        assert [f for f, _ in _refs(h["root"])] == ["R1", "#PWR01"]
        chan = _refs(h["chan"])
        assert chan[1][1] == [(uses[0], "R2", "1"), (uses[1], "R4", "1")]
        assert chan[0][1] == [(uses[0], "R3", "1"), (uses[1], "R5", "1")]

    def test_bad_scheme(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[])
        with pytest.raises(ValueError, match="scheme"):
            annotate_hierarchy(h["root"], scheme="page")


def _make_iface() -> Any:
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
    return iface


@pytest.mark.unit
class TestAnnotateHandler:
    def test_default_is_unchanged(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[])
        _write(h["root"], ROOT, _sym("R?", 50, 50, [h["root_path"]]), root=True)
        r = _make_iface()._handle_annotate_schematic({"schematicPath": str(h["root"])})
        assert r["success"] is True
        assert "scheme" not in r
        assert r["annotated"][0]["newReference"] == "R1"

    def test_sheet_x100_returns_mapping(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[])
        _write(h["root"], ROOT, _sym("R?", 50, 50, [h["root_path"]]), root=True)
        r = _make_iface()._handle_annotate_schematic(
            {"schematicPath": str(h["root"]), "scheme": "sheet_x100"}
        )
        assert r["success"] is True
        assert r["scheme"] == "sheet_x100" and r["reset"] is False and r["changed"] == 1
        assert r["annotated"][0]["newReference"] == "R101"
        assert r["annotated"][0]["instancePath"] == h["root_path"]

    def test_nothing_to_do(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[])
        _write(h["root"], ROOT, _sym("R101", 50, 50, [h["root_path"]]), root=True)
        r = _make_iface()._handle_annotate_schematic(
            {"schematicPath": str(h["root"]), "scheme": "sheet_x100"}
        )
        assert r["success"] is True and r["annotated"] == []

    def test_bad_scheme(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[])
        r = _make_iface()._handle_annotate_schematic(
            {"schematicPath": str(h["root"]), "scheme": "x100"}
        )
        assert r["success"] is False and "scheme" in r["message"]

    def test_bad_reset(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[])
        r = _make_iface()._handle_annotate_schematic(
            {"schematicPath": str(h["root"]), "reset": "yes"}
        )
        assert r["success"] is False and "reset" in r["message"]

    def test_non_numeric_page_is_an_error(self, tmp_path):
        h = _hierarchy(tmp_path, pages=[2])
        text = h["root"].read_text(encoding="utf-8").replace('(page "2")', '(page "A")')
        h["root"].write_text(text, encoding="utf-8")
        r = _make_iface()._handle_annotate_schematic(
            {"schematicPath": str(h["root"]), "scheme": "sheet_x100"}
        )
        assert r["success"] is False and "page" in r["message"]


# ---------------------------------------------------------------------------
# kicad-cli: the netlist after sheet_x100 on a real hierarchy
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_kicad_cli_netlist_after_sheet_x100(tmp_path):
    cli = resolve_kicad_cli()
    if not cli:
        pytest.skip("kicad-cli not available")
    version = subprocess.run([cli, "version"], capture_output=True, text=True).stdout
    if int(re.match(r"\s*(\d+)", version or "0").group(1)) < 9:
        pytest.skip("the fixture uses the KiCad 9 file format")
    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    loader = DynamicSymbolLoader(project_path=tmp_path)
    if loader.find_library_file("Device") is None or (
        loader.find_library_file("Amplifier_Operational") is None
    ):
        pytest.skip("KiCad's Device / Amplifier_Operational libraries not installed")

    (tmp_path / "design.kicad_pro").write_text("{}", encoding="utf-8")
    root = tmp_path / "design.kicad_sch"
    chan = tmp_path / "chan.kicad_sch"
    header = '(kicad_sch (version 20250114) (generator "eeschema") (generator_version "9.0")\n'
    root.write_text(
        header + f'\t(uuid "{ROOT}")\n\t(paper "A4")\n\t(lib_symbols)\n'
        '\t(sheet_instances (path "/" (page "1")))\n)\n',
        encoding="utf-8",
    )
    chan.write_text(header + f'\t(uuid "{CHAN}")\n\t(paper "A4")\n\t(lib_symbols)\n)\n')
    hier = SchematicHierarchyCommands(SimpleNamespace())
    for i in range(2):
        assert hier.add_hierarchical_sheet(
            {
                "schematicPath": str(root),
                "subsheetPath": str(chan),
                "sheetName": f"ch{i + 1}",
                "position": {"x": 30 + 60 * i, "y": 50},
            }
        )["success"]
    for reference, sheet, x, y in (
        ("R1", root, 50.8, 50.8),
        ("R2", root, 50.8, 76.2),
        ("R3", chan, 50.8, 50.8),
    ):
        loader.add_component(sheet, "Device", "R", reference=reference, value="10k", x=x, y=y)
    loader.add_component(
        chan, "Amplifier_Operational", "OPA4197xPW", reference="U1", x=101.6, y=101.6
    )
    uses = [u for u in sheet_instances(root) if u.file == "chan.kicad_sch"]
    for n, use in enumerate(uses, start=1):
        r = hier.set_symbol_instance(
            {
                "schematicPath": str(chan),
                "reference": "U1" if n == 1 else f"U{n}",
                "instancePath": use.path,
                "newReference": "U1",
                "newUnit": n,
            }
        )
        assert r["success"], r

    r = _make_iface()._handle_annotate_schematic(
        {"schematicPath": str(root), "scheme": "sheet_x100", "reset": True}
    )
    assert r["success"], r

    out = tmp_path / "design.net"
    run = subprocess.run(
        [cli, "sch", "export", "netlist", "-o", str(out), str(root)],
        capture_output=True,
        text=True,
    )
    log = run.stdout + run.stderr
    assert "Failed to load schematic" not in log and "annotation" not in log.lower(), log
    data = sexpdata.loads(out.read_text(encoding="utf-8"))
    comps = next(i for i in data[1:] if isinstance(i, list) and str(i[0]) == "components")
    refs = sorted(
        str(f[1]) for c in comps[1:] for f in c[1:] if isinstance(f, list) and str(f[0]) == "ref"
    )
    # Root (page 1): R101, R102. ch1 (page 2): R201, U201 (units A+B, numbered
    # on page 2). ch2 (page 3): R301.
    assert refs == ["R101", "R102", "R201", "R301", "U201"], refs
