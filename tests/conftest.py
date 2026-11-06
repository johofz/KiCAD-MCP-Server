"""
Test configuration for python/tests.

Sets up sys.modules stubs for heavy KiCAD modules (pcbnew, skip) before any
test module can trigger their import, preventing crashes on systems where the
real KiCAD environment is not fully initialised for testing.
"""

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# Make the repository's Python package tree importable in a clean checkout.
# Individual test modules must not depend on the caller's PYTHONPATH or on an
# editable install that CI does not perform.
_PYTHON_ROOT = Path(__file__).resolve().parents[1] / "python"
if str(_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(_PYTHON_ROOT))

# Skip the speculative full-symbol-library warm-up in tests. Each KiCADInterface
# would otherwise spawn a daemon thread parsing every installed .kicad_sym file;
# constructing an interface per test then piles up concurrent warm threads that
# saturate the CPU. On-demand parses still work (and are cached process-wide).
os.environ.setdefault("KICAD_SKIP_SYMBOL_WARMUP", "1")

# ---------------------------------------------------------------------------
# pcbnew stub — kicad_interface.py accesses pcbnew.__file__ and
# pcbnew.GetBuildVersion() at module level.  Use MagicMock so that any
# attribute access (pcbnew.BOARD, pcbnew.PCB_TRACK, …) returns a mock
# rather than raising AttributeError.
# ---------------------------------------------------------------------------


class _ModuleStub(MagicMock):
    """MagicMock that also keeps its child mocks in the instance ``__dict__``.

    Since pytest 9.2, ``monkeypatch.setattr`` saves the old value only from the
    instance ``__dict__``. A plain MagicMock keeps child mocks in
    ``_mock_children``, so ``undo()`` deleted the patched name instead of
    restoring it, and the stub then raised AttributeError for that name in
    every later test.
    """

    def __getattr__(self, name: str):
        child = super().__getattr__(name)
        self.__dict__[name] = child
        return child


if os.environ.get("KICAD_USE_REAL_PCBNEW") != "1":
    _pcbnew = _ModuleStub(name="pcbnew")
    _pcbnew.__file__ = "/fake/pcbnew.cpython-313-x86_64-linux-gnu.so"
    _pcbnew.__name__ = "pcbnew"
    _pcbnew.__spec__ = None
    _pcbnew.GetBuildVersion.return_value = "9.0.0-stub"
    sys.modules["pcbnew"] = _pcbnew

# ---------------------------------------------------------------------------
# Stub: skip  (kicad-skip — use real module if available, stub otherwise)
# ---------------------------------------------------------------------------
try:
    import skip as _skip_test  # noqa: F401 — try importing real skip
except ImportError:
    skip_mod = types.ModuleType("skip")

    class _FakeSchematic:
        """Minimal stand-in for skip.Schematic used in PinLocator cache."""

        def __init__(self, path: str):
            self.path = path
            self.symbol = []

    skip_mod.Schematic = _FakeSchematic  # type: ignore[attr-defined]
    sys.modules["skip"] = skip_mod


# ---------------------------------------------------------------------------
# kicad-cli integration tests: run each in both schematic file formats
# ---------------------------------------------------------------------------
import re as _re  # noqa: E402
import subprocess as _subprocess  # noqa: E402

import pytest  # noqa: E402


def _kicad_cli_major() -> int:
    from utils.kicad_cli import resolve_kicad_cli

    cli = resolve_kicad_cli()
    if not cli:
        return 0
    out = _subprocess.run([cli, "version"], capture_output=True, text=True).stdout
    m = _re.match(r"\s*(\d+)", out or "")
    return int(m.group(1)) if m else 0


class _SchFormat:
    """The schematic file format a kicad-cli test writes its fixtures in."""

    def __init__(self, major: int) -> None:
        self.major = major

    def restamp(self, *paths: Path) -> None:
        """Give each schematic on disk this format's header line."""
        from utils.sch_format import apply_schematic_header

        for path in paths:
            path.write_text(
                apply_schematic_header(path.read_text(encoding="utf-8")), encoding="utf-8"
            )


@pytest.fixture(params=[9, 10], ids=["kicad9-format", "kicad10-format"])
def sch_format(request, monkeypatch):
    """Run a kicad-cli test once per file format the installed KiCad can read.

    KiCad 10 still loads KiCad 9 files, so the KiCad 9 variant runs on both;
    the KiCad 10 variant is skipped where kicad-cli is older than 10. New
    schematics the tools create follow the same format (KICAD_SCH_FORMAT).
    """
    major = request.param
    cli_major = _kicad_cli_major()
    if cli_major == 0:
        pytest.skip("kicad-cli not available")
    if cli_major < major:
        pytest.skip(f"kicad-cli {cli_major} cannot read the KiCad {major} format")
    monkeypatch.setenv("KICAD_SCH_FORMAT", str(major))
    return _SchFormat(major)
