"""
Tests for list_sheet_instances and set_symbol_instance.

Covers:
  - utils.sheet_tree.sheet_instances: every use of every sheet, recursively,
    with KiCad's instance path, sheet name, file, page and parent path; reused
    sheets, nested sheets, KiCad 6 root-held page numbers, missing files
  - utils.symbol_instances.set_symbol_instance: editing / creating the
    (path ...) entry for one use, symbol selection by uuid or reference + unit,
    the first-use Reference field and own (unit N), error cases
  - Parameter validation in both handlers
  - kicad-cli: one quad op-amp symbol on a sheet used four times, unit A-D of
    one U501, netlists as ONE part with the right pins in each use (skipped
    where kicad-cli / KiCad's libraries are absent)
"""

import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
import sexpdata

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.schematic_hierarchy import SchematicHierarchyCommands  # noqa: E402
from utils.kicad_cli import resolve_kicad_cli  # noqa: E402
from utils.sheet_tree import sheet_instances  # noqa: E402
from utils.symbol_instances import (  # noqa: E402
    _placed_symbols,
    _read_part,
    set_symbol_instance,
)

ROOT = "aaaa0000-0000-0000-0000-000000000001"
CHAN = "bbbb0000-0000-0000-0000-000000000002"
LEAF = "cccc0000-0000-0000-0000-000000000003"


def _sch(own_uuid: str, *items: str, root: bool = False) -> str:
    tail = '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n' if root else ""
    return (
        '(kicad_sch\n\t(version 20250114)\n\t(generator "eeschema")\n'
        '\t(generator_version "9.0")\n'
        f'\t(uuid "{own_uuid}")\n\t(paper "A4")\n\t(lib_symbols)\n' + "".join(items) + tail + ")\n"
    )


def _symbol(symbol_uuid: str, reference: str, unit: int, entries: List[tuple], project="design"):
    paths = "".join(
        f'\t\t\t\t(path "{path}"\n\t\t\t\t\t(reference "{ref}")\n\t\t\t\t\t(unit {u})\n\t\t\t\t)\n'
        for path, ref, u in entries
    )
    instances = (
        f'\t\t(instances\n\t\t\t(project "{project}"\n{paths}\t\t\t)\n\t\t)\n' if entries else ""
    )
    return (
        '\t(symbol\n\t\t(lib_id "Amp:Quad")\n\t\t(at 100 100 0)\n'
        f"\t\t(unit {unit})\n"
        f'\t\t(uuid "{symbol_uuid}")\n'
        f'\t\t(property "Reference" "{reference}"\n\t\t\t(at 102 100 0)\n\t\t)\n'
        f"{instances}"
        "\t)\n"
    )


def _hierarchy(tmp_path: Path, uses: int = 2, nested: bool = False) -> Dict[str, Any]:
    """design.kicad_sch with chan.kicad_sch used *uses* times (and a leaf inside)."""
    (tmp_path / "design.kicad_pro").write_text("{}", encoding="utf-8")
    root = tmp_path / "design.kicad_sch"
    root.write_text(_sch(ROOT, root=True), encoding="utf-8")
    chan = tmp_path / "chan.kicad_sch"
    chan.write_text(_sch(CHAN), encoding="utf-8")
    hier = SchematicHierarchyCommands(SimpleNamespace())
    leaf = tmp_path / "sub" / "leaf.kicad_sch"
    if nested:
        leaf.parent.mkdir()
        leaf.write_text(_sch(LEAF), encoding="utf-8")
        r = hier.add_hierarchical_sheet(
            {"schematicPath": str(chan), "subsheetPath": str(leaf), "sheetName": "Leaf"}
        )
        assert r["success"], r
    for i in range(uses):
        r = hier.add_hierarchical_sheet(
            {
                "schematicPath": str(root),
                "subsheetPath": str(chan),
                "sheetName": f"ch{i + 1}",
                "position": {"x": 30 + 60 * i, "y": 50},
            }
        )
        assert r["success"], r
    return {"root": root, "chan": chan, "leaf": leaf, "instances": sheet_instances(root)}


def _entries(sheet: Path, symbol_uuid: str) -> List[tuple]:
    text = sheet.read_text(encoding="utf-8")
    for start, end in _placed_symbols(text):
        part = _read_part(text, start, end, "design")
        if part.uuid == symbol_uuid:
            return [(e.path, e.reference, e.unit) for e in part.entries]
    raise AssertionError(f"symbol {symbol_uuid} not found")


def _field_and_unit(sheet: Path, symbol_uuid: str) -> tuple:
    text = sheet.read_text(encoding="utf-8")
    for start, end in _placed_symbols(text):
        part = _read_part(text, start, end, "design")
        if part.uuid == symbol_uuid:
            return part.field, part.unit
    raise AssertionError(f"symbol {symbol_uuid} not found")


# ---------------------------------------------------------------------------
# sheet_instances
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSheetInstances:
    def test_root_only(self, tmp_path):
        root = tmp_path / "solo.kicad_sch"
        root.write_text(_sch(ROOT, root=True), encoding="utf-8")
        [inst] = sheet_instances(root)
        assert inst.path == f"/{ROOT}"
        assert inst.sheet_name == ""
        assert inst.file == "solo.kicad_sch"
        assert inst.page == "1"
        assert inst.parent_path is None

    def test_reused_sheet_is_listed_per_use(self, tmp_path):
        h = _hierarchy(tmp_path, uses=4)
        insts = h["instances"]
        assert len(insts) == 5
        children = insts[1:]
        assert [i.sheet_name for i in children] == ["ch1", "ch2", "ch3", "ch4"]
        assert [i.page for i in children] == ["2", "3", "4", "5"]
        assert {i.file for i in children} == {"chan.kicad_sch"}
        assert {i.parent_path for i in children} == {f"/{ROOT}"}
        assert len({i.path for i in children}) == 4
        for i in children:
            assert re.fullmatch(rf"/{ROOT}/[0-9a-f-]{{36}}", i.path)
            assert i.file_path == tmp_path / "chan.kicad_sch"

    def test_paths_match_the_symbol_instance_paths(self, tmp_path):
        from utils.sheet_tree import instance_paths

        h = _hierarchy(tmp_path, uses=3, nested=True)
        listed = {i.path for i in h["instances"]}
        _root, chan_paths = instance_paths(h["chan"])
        _root, leaf_paths = instance_paths(h["leaf"])
        assert set(chan_paths) <= listed
        assert set(leaf_paths) <= listed
        assert len(leaf_paths) == 3

    def test_nested_sheets_depth_first(self, tmp_path):
        h = _hierarchy(tmp_path, uses=2, nested=True)
        names = [(i.sheet_name, i.file) for i in h["instances"]]
        assert names == [
            ("", "design.kicad_sch"),
            ("ch1", "chan.kicad_sch"),
            ("Leaf", "sub/leaf.kicad_sch"),
            ("ch2", "chan.kicad_sch"),
            ("Leaf", "sub/leaf.kicad_sch"),
        ]
        leaf_insts = [i for i in h["instances"] if i.sheet_name == "Leaf"]
        parents = [i.path for i in h["instances"] if i.sheet_name in ("ch1", "ch2")]
        assert [i.parent_path for i in leaf_insts] == parents
        for leaf, parent in zip(leaf_insts, parents):
            assert leaf.path.startswith(parent + "/")

    def test_kicad6_pages_in_root_sheet_instances(self, tmp_path):
        block = (
            '\t(sheet (at 50 50) (size 30 20) (uuid "dddd0000-0000-0000-0000-00000000000d")\n'
            '\t\t(property "Sheet name" "Old" (at 50 49 0))\n'
            '\t\t(property "Sheet file" "old.kicad_sch" (at 50 71 0))\n\t)\n'
        )
        root = tmp_path / "legacy.kicad_sch"
        legacy_pages = (
            '\t(sheet_instances (path "/" (page "1"))'
            ' (path "/dddd0000-0000-0000-0000-00000000000d/" (page "7")))\n'
        )
        root.write_text(_sch(ROOT, block, legacy_pages), encoding="utf-8")
        insts = sheet_instances(root)
        assert [(i.sheet_name, i.page) for i in insts] == [("", "1"), ("Old", "7")]

    def test_missing_sub_sheet_file_is_listed(self, tmp_path):
        block = (
            '\t(sheet (at 50 50) (size 30 20) (uuid "eeee0000-0000-0000-0000-00000000000e")\n'
            '\t\t(property "Sheetname" "Gone" (at 50 49 0))\n'
            '\t\t(property "Sheetfile" "gone.kicad_sch" (at 50 71 0))\n'
            f'\t\t(instances (project "x" (path "/{ROOT}" (page "2"))))\n\t)\n'
        )
        root = tmp_path / "x.kicad_sch"
        root.write_text(_sch(ROOT, block, root=True), encoding="utf-8")
        insts = sheet_instances(root)
        assert [(i.sheet_name, i.page, i.file) for i in insts[1:]] == [
            ("Gone", "2", "gone.kicad_sch")
        ]

    def test_root_without_uuid(self, tmp_path):
        root = tmp_path / "old.kicad_sch"
        root.write_text("(kicad_sch (version 20211123))\n", encoding="utf-8")
        with pytest.raises(ValueError):
            sheet_instances(root)


@pytest.mark.unit
class TestListSheetInstancesHandler:
    def test_missing_path(self):
        r = SchematicHierarchyCommands(SimpleNamespace()).list_sheet_instances({})
        assert r["success"] is False
        assert "schematicPath" in r["message"]

    def test_missing_file(self, tmp_path):
        r = SchematicHierarchyCommands(SimpleNamespace()).list_sheet_instances(
            {"schematicPath": str(tmp_path / "nope.kicad_sch")}
        )
        assert r["success"] is False
        assert "not found" in r["message"]

    def test_fields(self, tmp_path):
        h = _hierarchy(tmp_path, uses=2)
        r = SchematicHierarchyCommands(SimpleNamespace()).list_sheet_instances(
            {"schematicPath": str(h["root"])}
        )
        assert r["success"] is True
        assert r["count"] == 3
        assert set(r["instances"][0]) == {
            "path",
            "sheetName",
            "file",
            "filePath",
            "page",
            "parentPath",
        }
        assert r["instances"][0]["parentPath"] is None
        assert r["instances"][1]["parentPath"] == f"/{ROOT}"
        assert r["instances"][2]["page"] == "3"


# ---------------------------------------------------------------------------
# set_symbol_instance
# ---------------------------------------------------------------------------

SYM = "11110000-0000-0000-0000-000000000001"
SYM_B = "22220000-0000-0000-0000-000000000002"


def _chan_with(tmp_path: Path, *symbols: str, uses: int = 4) -> Dict[str, Any]:
    h = _hierarchy(tmp_path, uses=uses)
    h["chan"].write_text(_sch(CHAN, *symbols), encoding="utf-8")
    h["paths"] = [i.path for i in h["instances"][1:]]
    return h


@pytest.mark.unit
class TestSetSymbolInstance:
    def _four(self, tmp_path):
        h = _hierarchy(tmp_path, uses=4)
        paths = [i.path for i in h["instances"][1:]]
        entries = [(p, f"U{n}", 1) for n, p in enumerate(paths, start=1)]
        h["chan"].write_text(_sch(CHAN, _symbol(SYM, "U1", 1, entries)), encoding="utf-8")
        h["paths"] = paths
        return h

    def test_one_symbol_four_units_one_reference(self, tmp_path):
        h = self._four(tmp_path)
        for n, path in enumerate(h["paths"], start=1):
            r = set_symbol_instance(h["chan"], path, uuid=SYM, new_reference="U501", new_unit=n)
            assert r["reference"] == "U501" and r["unit"] == n and r["created"] is False
        assert _entries(h["chan"], SYM) == [
            (p, "U501", str(n)) for n, p in enumerate(h["paths"], start=1)
        ]
        # The first use drives the Reference field and the symbol's own unit.
        assert _field_and_unit(h["chan"], SYM) == ("U501", "1")
        sexpdata.loads(h["chan"].read_text(encoding="utf-8"))

    def test_first_use_unit_follows(self, tmp_path):
        h = self._four(tmp_path)
        r = set_symbol_instance(h["chan"], h["paths"][0], uuid=SYM, new_unit=3)
        assert r["firstUse"] is True and r["oldUnit"] == 1
        assert _field_and_unit(h["chan"], SYM) == ("U1", "3")

    def test_other_use_leaves_field_and_own_unit(self, tmp_path):
        h = self._four(tmp_path)
        r = set_symbol_instance(h["chan"], h["paths"][2], uuid=SYM, new_reference="U9", new_unit=2)
        assert r["firstUse"] is False and r["oldReference"] == "U3"
        assert _field_and_unit(h["chan"], SYM) == ("U1", "1")
        assert _entries(h["chan"], SYM)[2] == (h["paths"][2], "U9", "2")

    def test_missing_entry_is_created(self, tmp_path):
        h = _hierarchy(tmp_path, uses=2)
        paths = [i.path for i in h["instances"][1:]]
        h["chan"].write_text(
            _sch(CHAN, _symbol(SYM, "U1", 1, [(paths[0], "U1", 1)])), encoding="utf-8"
        )
        r = set_symbol_instance(h["chan"], paths[1], uuid=SYM, new_unit=2)
        assert r["created"] is True
        assert _entries(h["chan"], SYM) == [(paths[0], "U1", "1"), (paths[1], "U1", "2")]

    def test_no_instances_block_is_created(self, tmp_path):
        h = _hierarchy(tmp_path, uses=2)
        paths = [i.path for i in h["instances"][1:]]
        h["chan"].write_text(_sch(CHAN, _symbol(SYM, "U7", 1, [])), encoding="utf-8")
        r = set_symbol_instance(h["chan"], paths[1], uuid=SYM, new_reference="U8", new_unit=2)
        assert r["created"] is True
        assert _entries(h["chan"], SYM) == [(paths[1], "U8", "2")]
        sexpdata.loads(h["chan"].read_text(encoding="utf-8"))

    def test_other_projects_block_is_left_alone(self, tmp_path):
        h = _hierarchy(tmp_path, uses=2)
        paths = [i.path for i in h["instances"][1:]]
        h["chan"].write_text(
            _sch(CHAN, _symbol(SYM, "U1", 1, [(paths[0], "U1", 1)], project="other")),
            encoding="utf-8",
        )
        set_symbol_instance(h["chan"], paths[0], uuid=SYM, new_reference="U5")
        text = h["chan"].read_text(encoding="utf-8")
        assert '(project "other"' in text and '(project "design"' in text
        assert _entries(h["chan"], SYM) == [(paths[0], "U5", "1")]
        assert re.search(r'\(project "other"\s+\(path "[^"]+"\s+\(reference "U1"\)', text)

    def test_select_by_reference(self, tmp_path):
        h = self._four(tmp_path)
        r = set_symbol_instance(h["chan"], h["paths"][1], reference="U2", new_unit=2)
        assert r["uuid"] == SYM

    def test_reference_with_several_units_needs_unit(self, tmp_path):
        h = _hierarchy(tmp_path, uses=1)
        path = h["instances"][1].path
        h["chan"].write_text(
            _sch(
                CHAN,
                _symbol(SYM, "U1", 1, [(path, "U1", 1)]),
                _symbol(SYM_B, "U1", 2, [(path, "U1", 2)]),
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="pass unit or uuid"):
            set_symbol_instance(h["chan"], path, reference="U1", new_reference="U3")
        r = set_symbol_instance(h["chan"], path, reference="U1", unit=2, new_reference="U3")
        assert r["uuid"] == SYM_B

    def test_unknown_symbol(self, tmp_path):
        h = self._four(tmp_path)
        with pytest.raises(ValueError, match="No placed symbol"):
            set_symbol_instance(h["chan"], h["paths"][0], reference="U99", new_unit=1)

    def test_path_that_is_not_a_use_of_the_sheet(self, tmp_path):
        h = self._four(tmp_path)
        with pytest.raises(ValueError, match="not a use"):
            set_symbol_instance(h["chan"], f"/{ROOT}", uuid=SYM, new_unit=2)

    def test_nothing_to_change(self, tmp_path):
        h = self._four(tmp_path)
        with pytest.raises(ValueError, match="Nothing to change"):
            set_symbol_instance(h["chan"], h["paths"][0], uuid=SYM)


@pytest.mark.unit
class TestSetSymbolInstanceHandler:
    def _call(self, **params):
        return SchematicHierarchyCommands(SimpleNamespace()).set_symbol_instance(params)

    def test_missing_schematic_path(self):
        r = self._call(uuid=SYM, instancePath="/x", newUnit=1)
        assert r["success"] is False and "schematicPath" in r["message"]

    def test_missing_symbol(self, tmp_path):
        r = self._call(schematicPath=str(tmp_path / "a.kicad_sch"), instancePath="/x", newUnit=1)
        assert r["success"] is False and "uuid or reference" in r["message"]

    @pytest.mark.parametrize("path", [None, "", "abc"])
    def test_bad_instance_path(self, tmp_path, path):
        r = self._call(schematicPath="x.kicad_sch", uuid=SYM, instancePath=path, newUnit=1)
        assert r["success"] is False and "instancePath" in r["message"]

    def test_nothing_to_change(self):
        r = self._call(schematicPath="x.kicad_sch", uuid=SYM, instancePath="/x")
        assert r["success"] is False and "newReference" in r["message"]

    @pytest.mark.parametrize("key,value", [("newUnit", 0), ("newUnit", "2"), ("unit", -1)])
    def test_bad_units(self, key, value):
        params = {"schematicPath": "x.kicad_sch", "reference": "U1", "instancePath": "/x"}
        params.setdefault("newReference", "U2")
        params[key] = value
        r = self._call(**params)
        assert r["success"] is False and key in r["message"]

    def test_empty_new_reference(self):
        r = self._call(schematicPath="x.kicad_sch", uuid=SYM, instancePath="/x", newReference=" ")
        assert r["success"] is False and "newReference" in r["message"]

    def test_missing_file(self, tmp_path):
        r = self._call(
            schematicPath=str(tmp_path / "nope.kicad_sch"), uuid=SYM, instancePath="/x", newUnit=1
        )
        assert r["success"] is False and "not found" in r["message"]

    def test_error_is_reported(self, tmp_path):
        h = _hierarchy(tmp_path, uses=1)
        r = self._call(
            schematicPath=str(h["chan"]), uuid=SYM, instancePath=h["instances"][1].path, newUnit=2
        )
        assert r["success"] is False and "No placed symbol" in r["message"]

    def test_success(self, tmp_path):
        h = _hierarchy(tmp_path, uses=1)
        path = h["instances"][1].path
        h["chan"].write_text(_sch(CHAN, _symbol(SYM, "U1", 1, [(path, "U1", 1)])), encoding="utf-8")
        r = self._call(schematicPath=str(h["chan"]), uuid=SYM, instancePath=path, newUnit=2)
        assert r["success"] is True
        assert r["unit"] == 2 and r["instancePath"] == path


# ---------------------------------------------------------------------------
# kicad-cli: one quad op-amp symbol, four sheet uses, one part
# ---------------------------------------------------------------------------


def _netlist(cli: str, root: Path) -> tuple:
    out = root.with_suffix(".net")
    run = subprocess.run(
        [cli, "sch", "export", "netlist", "-o", str(out), str(root)],
        capture_output=True,
        text=True,
    )
    assert out.exists(), run.stdout + run.stderr
    data = sexpdata.loads(out.read_text(encoding="utf-8"))
    sections = {str(i[0]): i for i in data[1:] if isinstance(i, list)}
    refs = []
    for comp in sections["components"][1:]:
        refs += [str(f[1]) for f in comp[1:] if isinstance(f, list) and str(f[0]) == "ref"]
    pins: Dict[str, set] = {}
    for net in sections["nets"][1:]:
        name = next(str(p[1]) for p in net[1:] if isinstance(p, list) and str(p[0]) == "name")
        for node in net[1:]:
            if isinstance(node, list) and str(node[0]) == "node":
                fields = {str(f[0]): str(f[1]) for f in node[1:] if isinstance(f, list)}
                pins.setdefault(name, set()).add((fields["ref"], fields["pin"]))
    return refs, pins, run.stdout + run.stderr


@pytest.mark.integration
def test_kicad_cli_one_quad_opamp_across_four_sheet_uses(tmp_path):
    cli = resolve_kicad_cli()
    if not cli:
        pytest.skip("kicad-cli not available")
    version = subprocess.run([cli, "version"], capture_output=True, text=True).stdout
    if int(re.match(r"\s*(\d+)", version or "0").group(1)) < 9:
        pytest.skip("the fixture uses the KiCad 9 file format")
    from commands.connection_schematic import ConnectionManager
    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    if DynamicSymbolLoader().find_library_file("Amplifier_Operational") is None:
        pytest.skip("KiCad's Amplifier_Operational symbol library not installed")

    h = _hierarchy(tmp_path, uses=4)
    DynamicSymbolLoader(project_path=tmp_path).add_component(
        h["chan"],
        "Amplifier_Operational",
        "OPA4197xPW",
        reference="U1",
        value="OPA4197",
        x=101.6,
        y=101.6,
        unit=1,
    )
    for pin, net in (("1", "OUT"), ("2", "INN"), ("3", "INP")):
        r = ConnectionManager.connect_to_net(h["chan"], "U1", pin, net)
        assert r is True or (isinstance(r, dict) and r.get("success")), r

    # Before: the sheet's four uses are four parts, U1..U4.
    refs, _pins, _log = _netlist(cli, h["root"])
    assert sorted(refs) == ["U1", "U2", "U3", "U4"]

    listed = SchematicHierarchyCommands(SimpleNamespace()).list_sheet_instances(
        {"schematicPath": str(h["root"])}
    )
    uses = [i for i in listed["instances"] if i["file"] == "chan.kicad_sch"]
    hier = SchematicHierarchyCommands(SimpleNamespace())
    for n, inst in enumerate(uses, start=1):
        r = hier.set_symbol_instance(
            {
                "schematicPath": inst["filePath"],
                "reference": f"U{n}",
                "instancePath": inst["path"],
                "newReference": "U501",
                "newUnit": n,
            }
        )
        assert r["success"], r

    refs, pins, log = _netlist(cli, h["root"])
    assert "Failed to load schematic" not in log
    assert refs == ["U501"], refs
    # OPA4197 (TSSOP-14): A = 1/2/3, B = 7/6/5, C = 8/9/10, D = 14/13/12 (out/-/+).
    expected = {1: ("1", "2", "3"), 2: ("7", "6", "5"), 3: ("8", "9", "10"), 4: ("14", "13", "12")}
    for n, inst in enumerate(uses, start=1):
        sheet = f"/{inst['sheetName']}/"
        for net, pin in zip(("OUT", "INN", "INP"), expected[n]):
            assert pins.get(sheet + net) == {("U501", pin)}, (sheet + net, pins.get(sheet + net))
