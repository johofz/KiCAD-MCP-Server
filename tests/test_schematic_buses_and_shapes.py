"""
Tests for add_schematic_bus, add_schematic_bus_entry, add_schematic_rectangle
and add_schematic_text_box.

Covers:
  - WireManager.add_bus / add_bus_entry S-expressions and segment splitting
    (KiCad attaches bus entries only to bus segment END points)
  - WireManager.add_rectangle / add_text_box in the KiCad 9 file format
    (stroke, fill, margins, the "no border" stroke width, text escaping)
  - Parameter validation in the four handlers
  - A kicad-cli load / ERC check (skipped where kicad-cli is unavailable)
"""

import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

import pytest
import sexpdata

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils.kicad_cli import resolve_kicad_cli  # noqa: E402

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_iface() -> Any:
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
    return iface


@pytest.fixture()
def iface():
    return _make_iface()


_MINIMAL_SCH = textwrap.dedent(
    """\
    (kicad_sch (version 20250114) (generator "eeschema") (generator_version "9.0")
    \t(uuid "aaaaaaaa-0000-0000-0000-000000000001")
    \t(paper "A4")
    \t(lib_symbols)
    \t(sheet_instances (path "/" (page "1")))
    )
    """
)


@pytest.fixture()
def sch(tmp_path) -> Path:
    path = tmp_path / "test.kicad_sch"
    path.write_text(_MINIMAL_SCH, encoding="utf-8")
    return path


def _items(path: Path, tag: str) -> List[list]:
    data = sexpdata.loads(path.read_text(encoding="utf-8"))
    return [i for i in data if isinstance(i, list) and i and i[0] == sexpdata.Symbol(tag)]


def _child(item: list, tag: str) -> list:
    return next(c for c in item[1:] if isinstance(c, list) and c and c[0] == sexpdata.Symbol(tag))


def _bus_segments(path: Path) -> List[tuple]:
    segments = []
    for bus in _items(path, "bus"):
        pts = _child(bus, "pts")
        (_, x1, y1), (_, x2, y2) = pts[1], pts[2]
        segments.append(((float(x1), float(y1)), (float(x2), float(y2))))
    return sorted(segments)


# ---------------------------------------------------------------------------
# WireManager.add_bus / add_bus_entry
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWireManagerBus:
    def test_one_segment_per_waypoint_pair(self, sch):
        from commands.wire_manager import WireManager

        result = WireManager.add_bus(sch, [[50.8, 50.8], [50.8, 101.6], [76.2, 101.6]])

        assert result == {
            "segments": [[50.8, 50.8, 50.8, 101.6], [50.8, 101.6, 76.2, 101.6]],
            "splitExisting": 0,
        }
        assert _bus_segments(sch) == [
            ((50.8, 50.8), (50.8, 101.6)),
            ((50.8, 101.6), (76.2, 101.6)),
        ]

    def test_bus_element_format(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_bus(sch, [[10, 10], [30, 10]])
        bus = _items(sch, "bus")[0]
        stroke = _child(bus, "stroke")
        assert _child(stroke, "width")[1] == 0
        assert str(_child(stroke, "type")[1]) == "default"
        assert re.fullmatch(r"[0-9a-f-]{36}", str(_child(bus, "uuid")[1]))

    def test_new_bus_is_split_at_existing_entries(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_bus_entry(sch, [50.8, 60.96], "down_right")
        WireManager.add_bus_entry(sch, [50.8, 81.28], "up_left")
        WireManager.add_bus(sch, [[50.8, 50.8], [50.8, 101.6]])

        assert _bus_segments(sch) == [
            ((50.8, 50.8), (50.8, 60.96)),
            ((50.8, 60.96), (50.8, 81.28)),
            ((50.8, 81.28), (50.8, 101.6)),
        ]

    def test_entry_at_a_waypoint_does_not_split_further(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_bus_entry(sch, [50.8, 101.6], "up_right")
        WireManager.add_bus(sch, [[50.8, 50.8], [50.8, 101.6], [76.2, 101.6]])

        assert len(_bus_segments(sch)) == 2

    def test_entry_off_the_bus_does_not_split(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_bus_entry(sch, [53.34, 60.96], "down_right")
        WireManager.add_bus(sch, [[50.8, 50.8], [50.8, 101.6]])

        assert len(_bus_segments(sch)) == 1

    def test_diagonal_bus_is_split_too(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_bus_entry(sch, [20, 20], "down_right")
        WireManager.add_bus(sch, [[10, 10], [30, 30]])

        assert _bus_segments(sch) == [((10, 10), (20, 20)), ((20, 20), (30, 30))]

    def test_new_bus_end_splits_existing_bus(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_bus(sch, [[10, 10], [10, 50]])
        result = WireManager.add_bus(sch, [[10, 30], [40, 30]])

        assert result["splitExisting"] == 1
        assert _bus_segments(sch) == [
            ((10, 10), (10, 30)),
            ((10, 30), (10, 50)),
            ((10, 30), (40, 30)),
        ]

    def test_bus_on_sub_sheet_without_sheet_instances(self, tmp_path):
        from commands.wire_manager import WireManager

        sub = tmp_path / "sub.kicad_sch"
        sub.write_text(
            '(kicad_sch (version 20250114) (generator "eeschema")\n'
            '\t(uuid "aaaaaaaa-0000-0000-0000-000000000009")\n\t(paper "A4")\n)\n',
            encoding="utf-8",
        )
        assert WireManager.add_bus(sub, [[0, 0], [10, 0]]) is not None
        assert len(_bus_segments(sub)) == 1


@pytest.mark.unit
class TestWireManagerBusEntry:
    @pytest.mark.parametrize(
        "direction,size,wire_end",
        [
            ("up_left", (-2.54, -2.54), (48.26, 58.42)),
            ("up_right", (2.54, -2.54), (53.34, 58.42)),
            ("down_left", (-2.54, 2.54), (48.26, 63.5)),
            ("down_right", (2.54, 2.54), (53.34, 63.5)),
        ],
    )
    def test_at_is_bus_point_and_size_points_to_wire_end(self, sch, direction, size, wire_end):
        from commands.wire_manager import WireManager

        result = WireManager.add_bus_entry(sch, [50.8, 60.96], direction)

        assert result["wireEnd"] == {"x": wire_end[0], "y": wire_end[1]}
        entry = _items(sch, "bus_entry")[0]
        assert [float(v) for v in _child(entry, "at")[1:]] == [50.8, 60.96]
        assert tuple(float(v) for v in _child(entry, "size")[1:]) == size
        stroke = _child(entry, "stroke")
        assert _child(stroke, "width")[1] == 0

    def test_existing_bus_is_split_at_the_entry(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_bus(sch, [[50.8, 50.8], [50.8, 101.6]])
        result = WireManager.add_bus_entry(sch, [50.8, 71.12], "down_right")

        assert result["splitExisting"] == 1
        assert _bus_segments(sch) == [
            ((50.8, 50.8), (50.8, 71.12)),
            ((50.8, 71.12), (50.8, 101.6)),
        ]
        uuids = {str(_child(b, "uuid")[1]) for b in _items(sch, "bus")}
        assert len(uuids) == 2

    def test_split_keeps_the_bus_stroke(self, sch):
        from commands.wire_manager import WireManager

        sch.write_text(
            _MINIMAL_SCH.replace(
                "\t(sheet_instances",
                "\t(bus (pts (xy 0 0) (xy 0 20)) (stroke (width 0.5) (type dash))"
                ' (uuid "bbbbbbbb-0000-0000-0000-000000000001"))\n\t(sheet_instances',
            ),
            encoding="utf-8",
        )
        WireManager.add_bus_entry(sch, [0, 10], "down_right")

        for bus in _items(sch, "bus"):
            stroke = _child(bus, "stroke")
            assert _child(stroke, "width")[1] == 0.5
            assert str(_child(stroke, "type")[1]) == "dash"

    def test_entry_at_bus_end_does_not_split(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_bus(sch, [[50.8, 50.8], [50.8, 101.6]])
        result = WireManager.add_bus_entry(sch, [50.8, 101.6], "up_right")

        assert result["splitExisting"] == 0
        assert len(_bus_segments(sch)) == 1


# ---------------------------------------------------------------------------
# WireManager.add_rectangle / add_text_box
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWireManagerRectangle:
    def test_default_dashed_frame(self, sch):
        from commands.wire_manager import WireManager

        assert WireManager.add_rectangle(sch, [10.0, 20.0], [110.16, 80.0])

        content = sch.read_text(encoding="utf-8")
        assert "(start 10 20)" in content
        assert "(end 110.16 80)" in content
        rect = _items(sch, "rectangle")[0]
        stroke = _child(rect, "stroke")
        assert _child(stroke, "width")[1] == 0.1524
        assert str(_child(stroke, "type")[1]) == "dash"
        assert str(_child(_child(rect, "fill"), "type")[1]) == "none"

    def test_custom_stroke_and_fill(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_rectangle(
            sch, [0, 0], [10, 10], stroke_width=0.3, stroke_type="dash_dot", fill="background"
        )
        rect = _items(sch, "rectangle")[0]
        stroke = _child(rect, "stroke")
        assert _child(stroke, "width")[1] == 0.3
        assert str(_child(stroke, "type")[1]) == "dash_dot"
        assert str(_child(_child(rect, "fill"), "type")[1]) == "background"

    def test_inserted_before_sheet_instances(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_rectangle(sch, [0, 0], [10, 10])
        content = sch.read_text(encoding="utf-8")
        assert content.index("(rectangle") < content.index("(sheet_instances")


@pytest.mark.unit
class TestWireManagerTextBox:
    def test_kicad9_text_box_layout(self, sch):
        from commands.wire_manager import WireManager

        assert WireManager.add_text_box(sch, "Note", [10.0, 20.0], [40.0, 15.0])

        box = _items(sch, "text_box")[0]
        assert box[1] == "Note"
        tags = [str(c[0]) for c in box[2:] if isinstance(c, list)]
        assert tags == [
            "exclude_from_sim",
            "at",
            "size",
            "margins",
            "stroke",
            "fill",
            "effects",
            "uuid",
        ]
        assert [float(v) for v in _child(box, "at")[1:]] == [10.0, 20.0, 0.0]
        assert [float(v) for v in _child(box, "size")[1:]] == [40.0, 15.0]
        # KiCad's default margin: half the stroke width + 0.75 x text height.
        assert [float(v) for v in _child(box, "margins")[1:]] == [0.9525] * 4
        stroke = _child(box, "stroke")
        assert _child(stroke, "width")[1] == 0
        assert str(_child(stroke, "type")[1]) == "solid"
        effects = _child(box, "effects")
        assert [str(v) for v in _child(effects, "justify")[1:]] == ["left", "top"]

    def test_no_border_uses_minus_one_iu(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_text_box(sch, "Plain", [0, 0], [20, 5], border=False)
        stroke = _child(_items(sch, "text_box")[0], "stroke")
        assert _child(stroke, "width")[1] == -0.0001

    def test_font_bold_and_justify(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_text_box(
            sch, "Big", [0, 0], [20, 5], font_size=2.54, bold=True, justify="center"
        )
        box = _items(sch, "text_box")[0]
        font = _child(_child(box, "effects"), "font")
        assert [float(v) for v in _child(font, "size")[1:]] == [2.54, 2.54]
        assert str(_child(font, "bold")[1]) == "yes"
        assert [str(v) for v in _child(_child(box, "effects"), "justify")[1:]] == ["top"]
        assert [float(v) for v in _child(box, "margins")[1:]] == [1.905] * 4

    def test_right_justify(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_text_box(sch, "R", [0, 0], [20, 5], justify="right")
        box = _items(sch, "text_box")[0]
        assert [str(v) for v in _child(_child(box, "effects"), "justify")[1:]] == ["right", "top"]

    def test_multiline_and_quotes_are_escaped(self, sch):
        from commands.wire_manager import WireManager

        WireManager.add_text_box(sch, 'line "one"\nline two', [0, 0], [30, 10])
        content = sch.read_text(encoding="utf-8")
        assert r'(text_box "line \"one\"\nline two"' in content
        assert _items(sch, "text_box")[0][1] == 'line "one"\nline two'


# ---------------------------------------------------------------------------
# Handler parameter validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandleAddSchematicBus:
    def test_missing_schematic_path(self, iface):
        result = iface._handle_add_schematic_bus({"waypoints": [[0, 0], [10, 0]]})
        assert result["success"] is False
        assert "schematicPath" in result["message"]

    @pytest.mark.parametrize(
        "waypoints", [None, [], [[0, 0]], [[0, 0], [1]], [[0, 0], ["a", 1]], "x"]
    )
    def test_bad_waypoints(self, iface, sch, waypoints):
        result = iface._handle_add_schematic_bus(
            {"schematicPath": str(sch), "waypoints": waypoints}
        )
        assert result["success"] is False
        assert "waypoint" in result["message"]

    def test_repeated_waypoint(self, iface, sch):
        result = iface._handle_add_schematic_bus(
            {"schematicPath": str(sch), "waypoints": [[0, 0], [0, 0]]}
        )
        assert result["success"] is False

    def test_missing_file(self, iface, tmp_path):
        result = iface._handle_add_schematic_bus(
            {"schematicPath": str(tmp_path / "nope.kicad_sch"), "waypoints": [[0, 0], [1, 0]]}
        )
        assert result["success"] is False
        assert "not found" in result["message"]

    def test_success(self, iface, sch):
        result = iface._handle_add_schematic_bus(
            {"schematicPath": str(sch), "waypoints": [[0, 0], [10, 0], [10, 10]]}
        )
        assert result["success"] is True
        assert len(result["segments"]) == 2


@pytest.mark.unit
class TestHandleAddSchematicBusEntry:
    def test_missing_schematic_path(self, iface):
        result = iface._handle_add_schematic_bus_entry({"position": [0, 0], "direction": "up_left"})
        assert result["success"] is False
        assert "schematicPath" in result["message"]

    def test_missing_position(self, iface, sch):
        result = iface._handle_add_schematic_bus_entry(
            {"schematicPath": str(sch), "direction": "up_left"}
        )
        assert result["success"] is False
        assert "position" in result["message"]

    @pytest.mark.parametrize("direction", [None, "up", "left", "UP_LEFT"])
    def test_bad_direction(self, iface, sch, direction):
        result = iface._handle_add_schematic_bus_entry(
            {"schematicPath": str(sch), "position": [0, 0], "direction": direction}
        )
        assert result["success"] is False
        assert "direction" in result["message"]

    def test_returns_wire_end(self, iface, sch):
        result = iface._handle_add_schematic_bus_entry(
            {"schematicPath": str(sch), "position": [50.8, 60.96], "direction": "down_left"}
        )
        assert result["success"] is True
        assert result["wireEnd"] == {"x": 48.26, "y": 63.5}


@pytest.mark.unit
class TestHandleAddSchematicRectangle:
    def test_missing_schematic_path(self, iface):
        result = iface._handle_add_schematic_rectangle({"start": [0, 0], "end": [1, 1]})
        assert result["success"] is False
        assert "schematicPath" in result["message"]

    def test_missing_corner(self, iface, sch):
        result = iface._handle_add_schematic_rectangle({"schematicPath": str(sch), "start": [0, 0]})
        assert result["success"] is False
        assert "end" in result["message"]

    def test_degenerate(self, iface, sch):
        result = iface._handle_add_schematic_rectangle(
            {"schematicPath": str(sch), "start": [0, 0], "end": [0, 10]}
        )
        assert result["success"] is False
        assert "non-zero" in result["message"]

    @pytest.mark.parametrize(
        "extra,word",
        [
            ({"strokeType": "dashed"}, "strokeType"),
            ({"fill": "color"}, "fill"),
            ({"strokeWidth": -1}, "strokeWidth"),
        ],
    )
    def test_bad_style(self, iface, sch, extra, word):
        params = {"schematicPath": str(sch), "start": [0, 0], "end": [10, 10], **extra}
        result = iface._handle_add_schematic_rectangle(params)
        assert result["success"] is False
        assert word in result["message"]

    def test_success(self, iface, sch):
        result = iface._handle_add_schematic_rectangle(
            {
                "schematicPath": str(sch),
                "start": [0, 0],
                "end": [10, 10],
                "strokeWidth": 0.254,
                "strokeType": "solid",
                "fill": "background",
            }
        )
        assert result["success"] is True
        assert "(type background)" in sch.read_text(encoding="utf-8")


@pytest.mark.unit
class TestHandleAddSchematicTextBox:
    def _params(self, sch, **extra):
        return {
            "schematicPath": str(sch),
            "text": "T",
            "position": [0, 0],
            "size": [10, 5],
            **extra,
        }

    def test_missing_schematic_path(self, iface):
        result = iface._handle_add_schematic_text_box(
            {"text": "T", "position": [0, 0], "size": [10, 5]}
        )
        assert result["success"] is False
        assert "schematicPath" in result["message"]

    def test_missing_text(self, iface, sch):
        result = iface._handle_add_schematic_text_box(self._params(sch, text=""))
        assert result["success"] is False
        assert "text" in result["message"]

    def test_missing_position(self, iface, sch):
        result = iface._handle_add_schematic_text_box(self._params(sch, position=None))
        assert result["success"] is False
        assert "position" in result["message"]

    @pytest.mark.parametrize("size", [None, [0, 5], [10, -1], [10]])
    def test_bad_size(self, iface, sch, size):
        result = iface._handle_add_schematic_text_box(self._params(sch, size=size))
        assert result["success"] is False
        assert "size" in result["message"]

    def test_bad_font_size(self, iface, sch):
        result = iface._handle_add_schematic_text_box(self._params(sch, fontSize=0))
        assert result["success"] is False
        assert "fontSize" in result["message"]

    def test_bad_justify(self, iface, sch):
        result = iface._handle_add_schematic_text_box(self._params(sch, justify="top"))
        assert result["success"] is False
        assert "justify" in result["message"]

    def test_success_without_border(self, iface, sch):
        result = iface._handle_add_schematic_text_box(
            self._params(sch, border=False, bold=True, fontSize=2, justify="right")
        )
        assert result["success"] is True
        assert "(width -0.0001)" in sch.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# kicad-cli: the elements load in KiCad 9 and the entries connect
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_kicad_cli_loads_buses_entries_and_shapes(tmp_path):
    cli = resolve_kicad_cli()
    if not cli:
        pytest.skip("kicad-cli not available")
    version = subprocess.run([cli, "version"], capture_output=True, text=True).stdout
    if int(re.match(r"\s*(\d+)", version or "0").group(1)) < 9:
        pytest.skip("the fixture uses the KiCad 9 file format")

    from commands.wire_manager import WireManager

    sch = tmp_path / "bus.kicad_sch"
    sch.write_text(_MINIMAL_SCH, encoding="utf-8")
    # Entries placed before and after the bus: both orders must end up on
    # segment end points.
    WireManager.add_bus_entry(sch, [50.8, 60.96], "down_right")
    WireManager.add_bus(sch, [[50.8, 50.8], [50.8, 101.6], [76.2, 101.6]])
    WireManager.add_bus_entry(sch, [50.8, 71.12], "down_right")
    WireManager.add_label(sch, "D[0..1]", [50.8, 55.88])
    for name, y in (("D0", 63.5), ("D1", 73.66)):
        WireManager.add_wire(sch, [53.34, y], [63.5, y])
        WireManager.add_label(sch, name, [63.5, y])
    WireManager.add_rectangle(sch, [40, 40], [100, 110])
    WireManager.add_text_box(sch, 'Bordered\n"box"', [120, 90], [40, 15], bold=True)
    WireManager.add_text_box(sch, "No border", [120, 110], [40, 10], border=False)

    report = tmp_path / "erc.rpt"
    run = subprocess.run(
        [cli, "sch", "erc", "-o", str(report), str(sch)], capture_output=True, text=True
    )
    assert "Failed to load schematic" not in run.stdout + run.stderr
    text = report.read_text(encoding="utf-8")
    # The two member labels lead nowhere (no pins), which is expected; the
    # entries themselves must be connected at both ends.
    assert "bus entry" not in text.lower(), text
    assert "Bus to Wire Entry" not in text, text
    assert _bus_segments(sch) == [
        ((50.8, 50.8), (50.8, 60.96)),
        ((50.8, 60.96), (50.8, 71.12)),
        ((50.8, 71.12), (50.8, 101.6)),
        ((50.8, 101.6), (76.2, 101.6)),
    ]

    svg_dir = tmp_path / "svg"
    run = subprocess.run(
        [cli, "sch", "export", "svg", "-o", str(svg_dir), str(sch)],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
