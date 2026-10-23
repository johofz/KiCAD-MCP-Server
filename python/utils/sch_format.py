"""File-format header for new schematics, matched to the installed KiCad.

A ``.kicad_sch`` declares its format in its first line. KiCad 10 writes
``(version 20260101)``; KiCad 9 writes ``(version 20250114)`` and refuses a
newer file with a bare "Failed to load schematic" -- in eeschema, in kicad-cli
(ERC, netlist export) and therefore in every tool that shells out to it.

New schematics therefore take the format of the KiCad the server runs against:

    1. Explicit override: ``$KICAD_SCH_FORMAT`` set to ``9`` or ``10``.
    2. The major version of the loaded ``pcbnew`` module.
    3. KiCad 10, when neither gives an answer.

Everything that later edits a schematic reads the declared version from the
file (see ``DynamicSymbolLoader._supports_kicad10_symbol_tokens``), so the
header written here decides which tokens the rest of the server emits.
"""

import logging
import os
import re
from typing import Optional

logger = logging.getLogger("kicad_interface")

KICAD9_SCH_VERSION = 20250114
KICAD10_SCH_VERSION = 20260101

_ENV_VAR = "KICAD_SCH_FORMAT"
_HEADER_RE = re.compile(
    r"\(kicad_sch\s+\(version\s+\d+\)\s+\(generator\s+\"[^\"]*\"\)"
    r"(\s+\(generator_version\s+\"[^\"]*\"\))?"
)


def _pcbnew_major() -> Optional[int]:
    """Major version of the importable pcbnew module, or None if unknown."""
    try:
        import pcbnew  # type: ignore

        version = pcbnew.Version()
    except Exception:
        return None
    m = re.match(r"\s*(\d+)\.", version) if isinstance(version, str) else None
    return int(m.group(1)) if m else None


def target_kicad_major() -> int:
    """KiCad major version (9 or 10) new schematics are written for."""
    override = os.environ.get(_ENV_VAR, "").strip()
    if override:
        if override in ("9", "10"):
            return int(override)
        logger.warning(f"Ignoring {_ENV_VAR}={override!r}: expected 9 or 10")
    major = _pcbnew_major()
    if major is not None and major <= 9:
        return 9
    return 10


def schematic_header() -> str:
    """First line of a new schematic, without a trailing newline."""
    if target_kicad_major() == 9:
        return (
            f'(kicad_sch (version {KICAD9_SCH_VERSION}) (generator "eeschema")'
            ' (generator_version "9.0")'
        )
    return (
        f'(kicad_sch (version {KICAD10_SCH_VERSION}) (generator "eeschema")'
        ' (generator_version "10.0")'
    )


def apply_schematic_header(content: str) -> str:
    """Replace the header of a schematic (e.g. a copied template) with ours."""
    return _HEADER_RE.sub(schematic_header(), content, count=1)
