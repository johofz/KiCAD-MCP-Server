"""
Shared text/S-expression helpers for schematic command modules.

These operate directly on ``.kicad_sch`` file text (no pcbnew / KiCADInterface needed)
and are used by the field-layout, batch-authoring and hierarchy command modules. Keeping
them here avoids those modules importing helpers from one another.
"""

import math
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from utils.sexpr_format import QUOTED_VALUE, unescape_sexpr_string

_KICAD_INTERNAL_PROPS = frozenset(
    {"ki_keywords", "ki_description", "ki_fp_filters", "ki_locked", "ki_model"}
)

# Paper sizes (landscape, mm). Border frame is ~12.7mm from each edge.
_PAPER_DIMS = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
    "A": (279.4, 215.9),
    "B": (431.8, 279.4),
    "C": (558.8, 431.8),
    "D": (863.6, 558.8),
    "E": (1117.6, 863.6),
}


def _find_matching_paren(s: str, start: int) -> int:
    """Return index of the ')' matching the '(' at position *start*, or -1."""
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _find_placed_symbol_block(
    content: str, reference: str, unit: Optional[int] = None
) -> Tuple[Optional[str], int, int]:
    """Find the placed symbol block for *reference*. Returns (block, start, end) or (None, -1, -1).

    ``unit`` picks one placement of a multi-unit part, whose units all carry the
    same reference; without it the first block in file order wins.
    """
    lib_sym_pos = content.find("(lib_symbols")
    lib_sym_end = _find_matching_paren(content, lib_sym_pos) if lib_sym_pos >= 0 else -1
    pattern = re.compile(r'\(symbol\s+\(lib_id\s+"')
    search_start = 0
    while True:
        m = pattern.search(content, search_start)
        if not m:
            break
        pos = m.start()
        if lib_sym_pos >= 0 and lib_sym_pos <= pos <= lib_sym_end:
            search_start = lib_sym_end + 1
            continue
        end = _find_matching_paren(content, pos)
        if end < 0:
            search_start = pos + 1
            continue
        block_text = content[pos : end + 1]
        if re.search(r'\(property\s+"Reference"\s+"' + re.escape(reference) + r'"', block_text):
            if unit is None:
                return block_text, pos, end
            um = re.search(r"\(unit\s+(\d+)\)", block_text)
            if um and int(um.group(1)) == int(unit):
                return block_text, pos, end
        search_start = end + 1
    return None, -1, -1


def _extract_component_properties(block_text: str, exclude_internal: bool = True) -> Dict[str, str]:
    """Extract {name: value} for all (property "name" "value" ...) entries in a symbol block."""
    props = {}
    for m in re.finditer(r"\(property\s+" + QUOTED_VALUE + r"\s+" + QUOTED_VALUE, block_text):
        name = unescape_sexpr_string(m.group(1))
        value = unescape_sexpr_string(m.group(2))
        if exclude_internal and name in _KICAD_INTERNAL_PROPS:
            continue
        props[name] = value
    return props


def _extract_property_position(block_text: str, property_name: str) -> Optional[Dict[str, float]]:
    """Return {"x","y","angle"} of a named property's (at ...), or None."""
    pat = re.compile(
        r'\(property\s+"'
        + re.escape(property_name)
        + r'"\s+"[^"]*"\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)'
    )
    m = pat.search(block_text)
    if m:
        return {"x": float(m.group(1)), "y": float(m.group(2)), "angle": float(m.group(3))}
    return None


def _extract_property_visible(block_text: str, property_name: str) -> bool:
    """True if the named property is visible (no hide flag)."""
    m = re.search(r'\(property\s+"' + re.escape(property_name) + r'"', block_text)
    if not m:
        return True
    end = _find_matching_paren(block_text, m.start())
    if end < 0:
        return True
    prop_sub = block_text[m.start() : end + 1]
    return "(hide yes)" not in prop_sub and "(hide)" not in prop_sub


def _get_sheet_usable_area(schematic_path) -> Tuple[float, float, float, float]:
    """Return (left, top, right, bottom) usable bounds in mm for the sheet's paper size."""
    border = 12.7
    width, height = 297.0, 210.0  # default A4
    try:
        with open(schematic_path, "r", encoding="utf-8") as f:
            content = f.read(4096)
        m = re.search(r'\(paper\s+"([^"]+)"', content)
        if m and m.group(1).strip() in _PAPER_DIMS:
            width, height = _PAPER_DIMS[m.group(1).strip()]
    except Exception:
        pass
    return (border, border, width - border, height - border)


def _apply_visibility(block: str, property_name: str, visible: bool) -> str:
    """Add or remove a (hide yes) flag on a property's (effects ...) sub-expression."""
    m = re.search(r'\(property\s+"' + re.escape(property_name) + r'"', block)
    if not m:
        return block
    ps = m.start()
    pe = _find_matching_paren(block, ps)
    if pe < 0:
        return block
    prop_sub = block[ps : pe + 1]
    is_hidden = "(hide yes)" in prop_sub or "(hide)" in prop_sub
    if not visible and not is_hidden:
        em = re.search(r"\(effects", prop_sub)
        if em:
            es = em.start()
            ee = _find_matching_paren(prop_sub, es)
            if ee >= 0:
                prop_sub = prop_sub[:es] + prop_sub[es:ee] + " (hide yes))" + prop_sub[ee + 1 :]
    elif visible and is_hidden:
        for tok in (" (hide yes)", "(hide yes) ", "(hide yes)", " (hide)", "(hide) ", "(hide)"):
            prop_sub = prop_sub.replace(tok, "")
    return block[:ps] + prop_sub + block[pe + 1 :]


def _symbol_orientation(block_text: str) -> Tuple[int, Optional[str]]:
    """(rotation in degrees, mirror axis "x"/"y"/None) of a placed symbol block."""
    m = re.search(
        r"^\(symbol\s+\(lib_id\s+\"[^\"]*\"\)\s+\(at\s+[-\d.]+\s+[-\d.]+\s+([-\d.]+)\)", block_text
    )
    rot = int(round(float(m.group(1)))) % 360 if m else 0
    mm = re.search(r"\(mirror\s+([xy])\)", block_text[: block_text.find("(property")])
    return rot, (mm.group(1) if mm else None)


def _stored_field_angle(block_text: str, orientation: str) -> int:
    """Field angle to store so the text reads *orientation* ("horizontal"/"vertical").

    eeschema rotates field text with the symbol: a symbol turned by 90 or 270
    degrees shows a 0-degree field vertically.
    """
    rot, _ = _symbol_orientation(block_text)
    turned = rot in (90, 270)
    return 90 if turned != (orientation == "vertical") else 0


def _stored_field_justify(block_text: str, justify: str) -> str:
    """Horizontal justification to store so the text appears *justify* on the sheet.

    eeschema transforms field justification with the symbol: rotation by 90 or
    180 degrees and a mirror about the Y axis each swap left and right
    (checked against kicad-cli 9 renders).
    """
    if justify not in ("left", "right"):
        return justify
    rot, mirror = _symbol_orientation(block_text)
    flipped = (rot in (90, 180)) != (mirror == "y")
    if not flipped:
        return justify
    return "right" if justify == "left" else "left"


def _apply_justify(block: str, property_name: str, justify: str) -> str:
    """Set the horizontal justification (left/right/center) in a property's effects."""
    m = re.search(r'\(property\s+"' + re.escape(property_name) + r'"', block)
    if not m:
        return block
    ps = m.start()
    pe = _find_matching_paren(block, ps)
    if pe < 0:
        return block
    prop_sub = block[ps : pe + 1]
    em = re.search(r"\(effects", prop_sub)
    if not em:
        return block
    es = em.start()
    ee = _find_matching_paren(prop_sub, es)
    if ee < 0:
        return block
    effects = prop_sub[es : ee + 1]
    jm = re.search(r"\s*\(justify((?:\s+\w+)*)\)", effects)
    vertical = []
    if jm:
        vertical = [t for t in jm.group(1).split() if t in ("top", "bottom", "mirror")]
        effects = effects[: jm.start()] + effects[jm.end() :]
    tokens = ([] if justify == "center" else [justify]) + vertical
    if tokens:
        effects = effects[:-1] + f" (justify {' '.join(tokens)}))"
    prop_sub = prop_sub[:es] + effects + prop_sub[ee + 1 :]
    return block[:ps] + prop_sub + block[pe + 1 :]


def _move_property_in_block(
    block_text, property_name, x, y, angle, visible, justify=None, orientation=None
) -> Tuple[str, int]:
    """Replace a property's (at ...) and apply visibility. Returns (new_block, n_substitutions).

    ``orientation`` ("horizontal"/"vertical") and ``justify`` ("left"/"right"/
    "center") describe the text as it appears on the sheet; they are converted
    for the symbol's rotation and mirroring. ``orientation`` overrides ``angle``.
    """
    if orientation:
        angle = _stored_field_angle(block_text, orientation)
    prop_pat = re.compile(
        r'(\(property\s+"'
        + re.escape(property_name)
        + r'"\s+"[^"]*"\s+)\(at\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\)'
    )
    new_block, n_subs = prop_pat.subn(r"\g<1>" + f"(at {x} {y} {angle})", block_text)
    if n_subs == 0:
        return block_text, 0
    if justify:
        new_block = _apply_justify(
            new_block, property_name, _stored_field_justify(block_text, justify)
        )
    return _apply_visibility(new_block, property_name, visible), n_subs


def _find_project_root(start_dir: Path) -> Path:
    """Walk up from *start_dir* to the nearest dir containing a .kicad_pro (else start_dir)."""
    current = start_dir.resolve()
    while True:
        if list(current.glob("*.kicad_pro")):
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return start_dir


def _find_facing_label(sch_path, net_name, position, orientation, proximity_mm=14.0):
    """Return [x, y] of an existing label for *net_name* that faces *position*, else None.

    "Facing" = within proximity_mm and oriented 180° opposite, so a single wire between
    the two pins is cleaner than two overlapping labels.
    """
    try:
        content = sch_path.read_text(encoding="utf-8")
        pat = re.compile(r'\(label\s+"([^"]+)"\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+([\d.]+)\)')
        px, py = float(position[0]), float(position[1])
        facing = (int(round(orientation)) % 360 + 180) % 360
        for m in pat.finditer(content):
            if m.group(1) != net_name:
                continue
            lx, ly, la = float(m.group(2)), float(m.group(3)), float(m.group(4))
            if math.hypot(lx - px, ly - py) > proximity_mm:
                continue
            if int(round(la)) % 360 == facing:
                return [lx, ly]
    except Exception:
        pass
    return None
