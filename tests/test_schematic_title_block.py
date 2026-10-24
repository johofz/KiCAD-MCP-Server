"""
set_schematic_title_block: title, date, revision, company and comments of one
sheet, written where and how eeschema writes them.
"""

import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils.kicad_cli import resolve_kicad_cli  # noqa: E402

SHEET = (
    "(kicad_sch\n"
    "\t(version 20250114)\n"
    '\t(generator "eeschema")\n'
    '\t(generator_version "9.0")\n'
    '\t(uuid "aaaaaaaa-0000-0000-0000-000000000001")\n'
    '\t(paper "A4")\n'
    "\t(lib_symbols)\n"
    '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n'
    "\t(embedded_fonts no)\n"
    ")\n"
)


def _make_iface() -> Any:
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
    return iface


@pytest.fixture()
def iface():
    return _make_iface()


@pytest.fixture()
def sch(tmp_path) -> Path:
    path = tmp_path / "t.kicad_sch"
    path.write_text(SHEET, encoding="utf-8")
    return path


def _set(iface, sch, **params):
    return iface._handle_set_schematic_title_block({"schematicPath": str(sch), **params})


@pytest.mark.unit
class TestSetTitleBlock:
    def test_block_goes_after_paper_in_eeschema_layout(self, iface, sch):
        result = _set(
            iface,
            sch,
            title="Photodiodenmodul – Analog",
            date="2026-10-24",
            revision="B",
            company="Goniophotometer",
            comments={"1": "4 Kanäle", "3": "ADS131M04"},
        )

        assert result["success"] is True
        text = sch.read_text(encoding="utf-8")
        assert (
            '\t(paper "A4")\n'
            "\t(title_block\n"
            '\t\t(title "Photodiodenmodul – Analog")\n'
            '\t\t(date "2026-10-24")\n'
            '\t\t(rev "B")\n'
            '\t\t(company "Goniophotometer")\n'
            '\t\t(comment 1 "4 Kanäle")\n'
            '\t\t(comment 3 "ADS131M04")\n'
            "\t)\n"
            "\t(lib_symbols)\n"
        ) in text
        assert result["titleBlock"]["revision"] == "B"
        assert result["titleBlock"]["comments"] == {"1": "4 Kanäle", "3": "ADS131M04"}

    def test_only_given_fields_change(self, iface, sch):
        _set(iface, sch, title="A", revision="A", comments={"1": "eins"})
        result = _set(iface, sch, revision="B")

        tb = result["titleBlock"]
        assert (tb["title"], tb["revision"], tb["comments"]) == ("A", "B", {"1": "eins"})
        assert sch.read_text(encoding="utf-8").count("(title_block") == 1

    def test_empty_string_removes_field_and_last_field_removes_block(self, iface, sch):
        _set(iface, sch, title="A", comments={"2": "zwei"})
        _set(iface, sch, comments={"2": ""})
        assert "(comment" not in sch.read_text(encoding="utf-8")

        _set(iface, sch, title="")
        assert sch.read_text(encoding="utf-8") == SHEET

    def test_quotes_and_backslashes_are_escaped(self, iface, sch):
        result = _set(iface, sch, title='Teil "A"\\B')

        assert '(title "Teil \\"A\\"\\\\B")' in sch.read_text(encoding="utf-8")
        assert result["titleBlock"]["title"] == 'Teil "A"\\B'

    def test_comment_list_counts_from_one(self, iface, sch):
        result = _set(iface, sch, comments=["eins", "zwei"])
        assert result["titleBlock"]["comments"] == {"1": "eins", "2": "zwei"}

    def test_comment_number_out_of_range(self, iface, sch):
        result = _set(iface, sch, comments={"10": "x"})
        assert result["success"] is False

    def test_missing_file(self, iface, tmp_path):
        result = _set(iface, tmp_path / "nope.kicad_sch", title="A")
        assert result["success"] is False


@pytest.mark.integration
def test_kicad_cli_reads_the_title_block(tmp_path, iface):
    cli = resolve_kicad_cli()
    if not cli:
        pytest.skip("kicad-cli not available")
    version = subprocess.run([cli, "version"], capture_output=True, text=True).stdout
    if int(re.match(r"\s*(\d+)", version or "0").group(1)) < 9:
        pytest.skip("the fixture uses the KiCad 9 file format")
    sch = tmp_path / "t.kicad_sch"
    sch.write_text(SHEET, encoding="utf-8")
    _set(iface, sch, title="Titel X", revision="R7", company="Firma Y", comments={"1": "K1"})

    subprocess.run(
        [cli, "sch", "export", "svg", "-o", str(tmp_path), str(sch)],
        check=True,
        capture_output=True,
    )
    svg = next(tmp_path.glob("*.svg")).read_text(encoding="utf-8")
    # kicad-cli keeps drawn text searchable in the SVG
    for text in ("Titel X", "R7", "Firma Y", "K1"):
        assert text in svg
