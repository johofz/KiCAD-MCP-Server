"""
Field orientation and justification "as shown on the sheet".

eeschema transforms a field's text angle and horizontal justification with
its symbol. The expected stored values below were read off kicad-cli 9
renders of Device:R in every rotation and mirror used here.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.schematic_text_utils import _move_property_in_block  # noqa: E402


def _block(rot: int, mirror: str = "", justify: str = "") -> str:
    m = f"\n\t\t(mirror {mirror})" if mirror else ""
    j = f" (justify {justify})" if justify else ""
    return (
        f'(symbol\n\t\t(lib_id "Device:R")\n\t\t(at 50 50 {rot}){m}\n\t\t(unit 1)\n'
        f'\t\t(property "Reference" "R1"\n\t\t\t(at 52 50 0)\n'
        f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t){j}\n\t\t\t)\n\t\t)\n"
        f'\t\t(property "Value" "10k"\n\t\t\t(at 54 50 0)\n'
        f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)"
    )


def _ref_prop(block: str) -> str:
    start = block.index('(property "Reference"')
    return block[start : block.index('(property "Value"')]


@pytest.mark.unit
@pytest.mark.parametrize(
    "rot,mirror,stored",
    [
        (0, "", "left"),
        (90, "", "right"),
        (180, "", "right"),
        (270, "", "left"),
        (0, "x", "left"),
        (0, "y", "right"),
        (90, "x", "right"),
        (90, "y", "left"),
        (180, "x", "right"),
        (270, "x", "left"),
    ],
)
def test_left_on_sheet_is_converted_for_symbol_transform(rot, mirror, stored) -> None:
    new, n = _move_property_in_block(
        _block(rot, mirror), "Reference", 52, 48, 0, True, justify="left"
    )
    assert n == 1
    assert f"(justify {stored})" in _ref_prop(new)


@pytest.mark.unit
@pytest.mark.parametrize("rot,angle", [(0, 0), (90, 90), (180, 0), (270, 90)])
def test_horizontal_orientation_compensates_rotation(rot, angle) -> None:
    new, _ = _move_property_in_block(
        _block(rot), "Reference", 52, 48, 0, True, orientation="horizontal"
    )
    assert f"(at 52 48 {angle})" in _ref_prop(new)


@pytest.mark.unit
def test_vertical_orientation_on_unrotated_symbol() -> None:
    new, _ = _move_property_in_block(
        _block(0), "Reference", 52, 48, 0, True, orientation="vertical"
    )
    assert "(at 52 48 90)" in _ref_prop(new)


@pytest.mark.unit
def test_center_removes_horizontal_justify_but_keeps_vertical() -> None:
    new, _ = _move_property_in_block(
        _block(0, justify="left bottom"), "Reference", 52, 48, 0, True, justify="center"
    )
    prop = _ref_prop(new)
    assert "left" not in prop
    assert "(justify bottom)" in prop


@pytest.mark.unit
def test_justify_replaces_existing_and_leaves_other_field_alone() -> None:
    new, _ = _move_property_in_block(
        _block(0, justify="right"), "Reference", 52, 48, 0, True, justify="left"
    )
    assert _ref_prop(new).count("justify") == 1
    assert "(justify left)" in _ref_prop(new)
    assert "justify" not in new[new.index('(property "Value"') :]
    assert new.count("(") == new.count(")")


@pytest.mark.unit
def test_without_justify_the_effects_are_untouched() -> None:
    block = _block(90, justify="left")
    new, _ = _move_property_in_block(block, "Reference", 52, 48, 0, True)
    assert "(justify left)" in _ref_prop(new)
