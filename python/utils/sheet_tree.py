"""Walk a schematic hierarchy from its root sheet.

The design is the set of sheets reachable from the root through ``(sheet ...)``
references, and nothing else. Globbing the project directory for ``*.kicad_sch``
looks equivalent and is not: KiCad's own Local History (``.history/``), the
``.mcp-backups/`` copies this server writes, hand-made backup folders and any
unrelated project below the directory all match the glob. Read as live sheets
they inject nets and parts the design no longer has, and whichever copy sorts
last overwrites the live sheet's nets (#400 -- 25 wrong pad nets on a real
board). Rewritten as live sheets they destroy the fallback at the moment the
risky edit happens (#365).

Shared by ``backannotate_footprints`` (writes) and ``sync_schematic_to_board``
(reads); anything that needs "the sheets of this design" should use it too.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, FrozenSet, List, NamedTuple, Optional, Set, Tuple

from utils.file_io import read_text_preserve_newline
from utils.sexpr_format import QUOTED_VALUE, iter_child_offsets, match_paren, unescape_sexpr_string

_SHEET_HEAD = re.compile(r"\(sheet[\s(]")
_PROPERTY_HEAD = re.compile(rf"\(property\s+{QUOTED_VALUE}\s+{QUOTED_VALUE}")
# Quoted (KiCad 10) or bare (older writers); any token, as sexpdata reads it.
_UUID_HEAD = re.compile(r'\(uuid\s+"?([^\s()"]+)"?\s*\)')
_REFERENCE = re.compile(rf"\(reference\s+{QUOTED_VALUE}\s*\)")
_REFERENCE_FIELD = re.compile(rf'\(property\s+"Reference"\s+{QUOTED_VALUE}')

#: How many directories to search for a project's root: the sheet's own, then
#: up to three parents, stopping at the first that holds a ``.kicad_pro``.
_PROJECT_SEARCH_DIRS = 4


def sub_sheet_files(text: str) -> List[str]:
    """The ``Sheetfile`` of every top-level ``(sheet ...)`` in one schematic.

    Older files spell the property ``Sheet file``; both are accepted. Values
    are unescaped, so a file name containing a quote round-trips.
    """
    files: List[str] = []
    for offset in iter_child_offsets(text):
        if not _SHEET_HEAD.match(text, offset):
            continue
        end = match_paren(text, offset)
        if end == -1:
            continue
        block = text[offset : end + 1]
        for prop_offset in iter_child_offsets(block):
            m = _PROPERTY_HEAD.match(block, prop_offset)
            if not m:
                continue
            name = unescape_sexpr_string(m.group(1))
            if name in ("Sheetfile", "Sheet file"):
                value = unescape_sexpr_string(m.group(2))
                if value:
                    files.append(value)
                break
    return files


def _real_key(path: Path) -> str:
    """Identity of a file on disk, so the same sheet is never visited twice."""
    return os.path.normcase(os.path.realpath(str(path)))


def sheet_tree(root: Path) -> List[Path]:
    """Sheets reachable from *root*, root first, one entry per file on disk.

    A sheet file referenced twice (a reused sub-sheet) is listed once. A
    reference to a file that does not exist is skipped; KiCad reports that as a
    missing sheet and so should the tool that owns the operation, not this
    walker. Unreadable files are skipped for the same reason.
    """
    order: List[Path] = []
    seen: Set[str] = set()
    queue: List[Path] = [Path(root)]
    while queue:
        sheet = queue.pop(0)
        key = _real_key(sheet)
        if key in seen:
            continue
        seen.add(key)
        if not sheet.is_file():
            continue
        order.append(sheet)
        try:
            text, _newline = read_text_preserve_newline(sheet)
        except (OSError, UnicodeDecodeError):
            continue
        for name in sub_sheet_files(text):
            queue.append(sheet.parent / name)
    return order


# --------------------------------------------------------------------------- #
# Instance paths
# --------------------------------------------------------------------------- #
#
# Every placed symbol records, for each use of its sheet, the path of that use:
# the root sheet's uuid, then the uuid of each (sheet ...) block on the way
# down, e.g. /<root>/<block> for a sheet placed on the root. A sheet placed
# twice has two paths, and so does everything on it; so does a sheet placed
# once inside a sheet that is placed twice (#428).


def sub_sheets(text: str) -> List[Tuple[str, str]]:
    """``(block uuid, Sheetfile)`` for every top-level ``(sheet ...)`` in one schematic.

    The block's uuid is the path element KiCad uses for everything on that
    use of the sheet. A block missing either is skipped. Both spellings of the
    file property are accepted, as in ``sub_sheet_files``.
    """
    found: List[Tuple[str, str]] = []
    for offset in iter_child_offsets(text):
        if not _SHEET_HEAD.match(text, offset):
            continue
        end = match_paren(text, offset)
        if end == -1:
            continue
        block = text[offset : end + 1]
        block_uuid = file_name = ""
        for child in iter_child_offsets(block):
            m = _UUID_HEAD.match(block, child)
            if m:
                block_uuid = m.group(1)
                continue
            m = _PROPERTY_HEAD.match(block, child)
            if m and unescape_sexpr_string(m.group(1)) in ("Sheetfile", "Sheet file"):
                file_name = unescape_sexpr_string(m.group(2))
        if block_uuid and file_name:
            found.append((block_uuid, file_name))
    return found


def sheet_uuid(text: str) -> str:
    """A schematic's own top-level ``(uuid ...)``, or '' if it has none."""
    for offset in iter_child_offsets(text):
        m = _UUID_HEAD.match(text, offset)
        if m:
            return m.group(1)
    return ""


class SheetInfo(NamedTuple):
    """What the hierarchy walk needs from one schematic file."""

    uuid: str
    sub_sheets: Tuple[Tuple[str, str], ...]
    #: ``(sheet_instances ...)`` is present. KiCad writes it only into the
    #: root, but create_schematic writes it into every file, so it is a hint.
    has_sheet_instances: bool
    #: Every reference in the file, from instance entries and Reference fields.
    references: FrozenSet[str]


# real path -> ((mtime_ns, size), info). A sheet is parsed again only when it
# changes, so placing parts one call at a time does not rescan the whole
# project for every part.
_INFO_CACHE: Dict[str, Tuple[Tuple[int, int], SheetInfo]] = {}


def sheet_info(sheet: Path) -> Optional[SheetInfo]:
    """The hierarchy facts of *sheet*, or None if it cannot be read."""
    try:
        st = os.stat(sheet)
    except OSError:
        return None
    key = _real_key(sheet)
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _INFO_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        text, _newline = read_text_preserve_newline(Path(sheet))
    except (OSError, UnicodeDecodeError):
        return None
    references = {unescape_sexpr_string(v) for v in _REFERENCE.findall(text)}
    references.update(unescape_sexpr_string(v) for v in _REFERENCE_FIELD.findall(text))
    info = SheetInfo(
        uuid=sheet_uuid(text),
        sub_sheets=tuple(sub_sheets(text)),
        has_sheet_instances="(sheet_instances" in text,
        references=frozenset(references),
    )
    _INFO_CACHE[key] = (stamp, info)
    return info


def project_name(sheet: Path) -> str:
    """The project name KiCad records in ``(instances (project "<name>" ...))``.

    That is the stem of the nearest ``.kicad_pro``, in the sheet's directory
    or up to three parents, so a sheet kept in a subdirectory gets its
    project's name. When one directory holds several projects (KiCad's ecc83
    demo has ecc83-pp and ecc83-pp_v2), the one whose root reaches the sheet
    wins: KiCad ignores instance data filed under another project's name.
    Falls back to the sheet's own stem.
    """
    try:
        resolved = Path(sheet).resolve()
        search = [resolved.parent, *list(resolved.parent.parents)[: _PROJECT_SEARCH_DIRS - 1]]
        for directory in search:
            projects = sorted(directory.glob("*.kicad_pro"))
            if len(projects) > 1:
                for pro in projects:
                    root = pro.with_suffix(".kicad_sch")
                    if root.is_file() and sheet_chains(root, resolved):
                        return pro.stem
            if projects:
                return projects[0].stem
        return resolved.stem
    except Exception:
        return Path(sheet).stem


def root_candidates(target: Path) -> List[Path]:
    """Possible root schematics of *target*'s project, most likely first.

    The search runs from *target*'s directory upward, up to three parents,
    and stops at the first directory that holds a ``.kicad_pro``. A sub-sheet
    may live in a subdirectory of the project, and a search confined to its
    own directory never found the root (#428).

    Schematics named after a ``.kicad_pro`` come first. Then come schematics
    that carry ``(sheet_instances ...)`` and that no scanned schematic
    references; that is only a hint (see SheetInfo). *target* is never in the
    second group, or a sub-sheet that looks unlinked from its own directory
    would stand in for the root above it. Callers keep the first candidate
    whose sheet tree reaches *target*.
    """
    target_key = _real_key(target)
    named: List[Path] = []
    bearers: List[Path] = []
    referenced: Set[str] = set()
    directory = Path(target).resolve().parent
    for _ in range(_PROJECT_SEARCH_DIRS):
        projects = sorted(directory.glob("*.kicad_pro"))
        for pro in projects:
            candidate = directory / f"{pro.stem}.kicad_sch"
            if candidate.is_file():
                named.append(candidate)
        for sch in sorted(directory.glob("*.kicad_sch")):
            info = sheet_info(sch)
            if info is None:
                continue
            referenced.update(_real_key(sch.parent / name) for _, name in info.sub_sheets)
            if info.has_sheet_instances:
                bearers.append(sch)
        if projects or directory.parent == directory:
            break
        directory = directory.parent

    unreferenced = [
        b for b in bearers if _real_key(b) not in referenced and _real_key(b) != target_key
    ]
    candidates: List[Path] = []
    seen: Set[str] = set()
    for candidate in named + unreferenced:
        key = _real_key(candidate)
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)
    return candidates


def sheet_chains(root: Path, target: Path) -> List[List[str]]:
    """Every uuid chain from *root* down to *target*: ``[root, block, ...]``.

    One chain per use of *target*, in the order the sheets appear in their
    parents, or [] when *root* does not reach *target*.
    """
    root_info = sheet_info(root)
    if root_info is None or not root_info.uuid:
        return []
    target_key = _real_key(target)
    if _real_key(root) == target_key:
        return [[root_info.uuid]]

    chains: List[List[str]] = []

    def walk(sheet: Path, info: SheetInfo, chain: List[str], ancestors: FrozenSet[str]) -> None:
        for block_uuid, name in info.sub_sheets:
            child = sheet.parent / name
            key = _real_key(child)
            if key == target_key:
                chains.append(chain + [block_uuid])
                continue
            if key in ancestors:
                continue  # a sheet that contains itself; KiCad refuses to load that
            child_info = sheet_info(child)
            if child_info is not None:
                walk(child, child_info, chain + [block_uuid], ancestors | {key})

    walk(Path(root), root_info, [root_info.uuid], frozenset({_real_key(root)}))
    return chains


def instance_paths(target: Path) -> Tuple[Optional[Path], List[str]]:
    """``(root, paths)``: the instance path of every use of *target*.

    ``/<root-uuid>`` for the root itself and ``/<root-uuid>/<block>[/...]``
    for a sheet below it, one path per use. When no root reaches *target* (a
    sheet not linked yet), root is None and the one path is
    ``/<target-uuid>``; add_hierarchical_sheet adds the real paths once the
    sheet is linked.
    """
    for root in root_candidates(target):
        chains = sheet_chains(root, target)
        if chains:
            return root, ["/" + "/".join(chain) for chain in chains]
    info = sheet_info(target)
    own = info.uuid if info is not None else ""
    return None, [f"/{own}" if own else "/"]


# --------------------------------------------------------------------------- #
# Sheet instances with page numbers
# --------------------------------------------------------------------------- #
#
# KiCad 7+ files a sheet's page number inside its (sheet ...) block, once per
# use of the parent: (instances (project "<name>" (path "/<chain to the
# parent>" (page "N")))). The root's own page is in its (sheet_instances
# (path "/" (page "1"))). KiCad 6 kept every page in the root's
# (sheet_instances ...) under paths without the root uuid ("/<block>/"), which
# is read as a fallback.

_INSTANCES_HEAD = re.compile(r"\(instances[\s(]")
_SHEET_INSTANCES_HEAD = re.compile(r"\(sheet_instances[\s(]")
_PROJECT_HEAD = re.compile(rf"\(project\s+{QUOTED_VALUE}")
_PATH_HEAD = re.compile(rf"\(path\s+{QUOTED_VALUE}")
_PAGE = re.compile(rf"\(page\s+{QUOTED_VALUE}\s*\)")


class SheetInstance(NamedTuple):
    """One use of a sheet in the hierarchy."""

    #: Instance path as symbol (instances ...) entries use it: "/<root uuid>"
    #: for the root, "/<root uuid>/<block uuid>[/...]" below it.
    path: str
    #: The block's Sheetname ("" for the root).
    sheet_name: str
    #: Sheetfile as written in the parent block (the root's own file name).
    file: str
    file_path: Path
    page: str
    #: Path of the parent instance; None for the root.
    parent_path: Optional[str]


def _pages_by_path(block: str, project: str) -> Dict[str, str]:
    """``{parent path: page}`` from a (sheet ...) block's (instances ...)."""
    for offset in iter_child_offsets(block):
        if not _INSTANCES_HEAD.match(block, offset):
            continue
        inst = block[offset : match_paren(block, offset) + 1]
        chosen = None
        for p_offset in iter_child_offsets(inst):
            m = _PROJECT_HEAD.match(inst, p_offset)
            if not m:
                continue
            span = inst[p_offset : match_paren(inst, p_offset) + 1]
            if unescape_sexpr_string(m.group(1)) == project:
                chosen = span
                break
            if chosen is None:
                chosen = span  # KiCad's own fallback: the first project
        pages: Dict[str, str] = {}
        if chosen is not None:
            for e_offset in iter_child_offsets(chosen):
                m = _PATH_HEAD.match(chosen, e_offset)
                if not m:
                    continue
                entry = chosen[e_offset : match_paren(chosen, e_offset) + 1]
                page = _PAGE.search(entry)
                if page:
                    pages[unescape_sexpr_string(m.group(1))] = unescape_sexpr_string(page.group(1))
        return pages
    return {}


def _root_sheet_instance_pages(text: str) -> Dict[str, str]:
    """``{path: page}`` from a root's (sheet_instances ...) block."""
    for offset in iter_child_offsets(text):
        if not _SHEET_INSTANCES_HEAD.match(text, offset):
            continue
        block = text[offset : match_paren(text, offset) + 1]
        pages: Dict[str, str] = {}
        for e_offset in iter_child_offsets(block):
            m = _PATH_HEAD.match(block, e_offset)
            if not m:
                continue
            entry = block[e_offset : match_paren(block, e_offset) + 1]
            page = _PAGE.search(entry)
            if page:
                pages[unescape_sexpr_string(m.group(1))] = unescape_sexpr_string(page.group(1))
        return pages
    return {}


def _sheet_blocks(text: str) -> List[Tuple[str, str, str, str]]:
    """``(block uuid, Sheetname, Sheetfile, block text)`` per top-level sheet."""
    found: List[Tuple[str, str, str, str]] = []
    for offset in iter_child_offsets(text):
        if not _SHEET_HEAD.match(text, offset):
            continue
        end = match_paren(text, offset)
        if end == -1:
            continue
        block = text[offset : end + 1]
        block_uuid = name = file_name = ""
        for child in iter_child_offsets(block):
            m = _UUID_HEAD.match(block, child)
            if m:
                block_uuid = m.group(1)
                continue
            m = _PROPERTY_HEAD.match(block, child)
            if not m:
                continue
            key = unescape_sexpr_string(m.group(1))
            if key in ("Sheetfile", "Sheet file"):
                file_name = unescape_sexpr_string(m.group(2))
            elif key in ("Sheetname", "Sheet name"):
                name = unescape_sexpr_string(m.group(2))
        if block_uuid and file_name:
            found.append((block_uuid, name, file_name, block))
    return found


def sheet_instances(root: Path) -> List[SheetInstance]:
    """Every use of every sheet reachable from *root*, root first.

    Depth first, in the order the blocks appear in their parents, so a sheet
    used four times is listed four times, each with its own path and page.
    A block whose file is missing is still listed (its children cannot be).
    A sheet that contains itself is not followed again.
    """
    root = Path(root)
    text, _newline = read_text_preserve_newline(root)
    root_uuid = sheet_uuid(text)
    if not root_uuid:
        raise ValueError(f"{root} has no (uuid ...); not a KiCad 6+ schematic")
    project = project_name(root)
    legacy = _root_sheet_instance_pages(text)
    result: List[SheetInstance] = [
        SheetInstance(
            path=f"/{root_uuid}",
            sheet_name="",
            file=root.name,
            file_path=root,
            page=legacy.get("/", "1"),
            parent_path=None,
        )
    ]

    def walk(sheet: Path, sheet_text: str, path: str, chain: List[str], ancestors: Set[str]):
        for block_uuid, name, file_name, block in _sheet_blocks(sheet_text):
            child = sheet.parent / file_name
            child_path = f"{path}/{block_uuid}"
            pages = _pages_by_path(block, project)
            # KiCad 6: "/<block>/.../" in the root's (sheet_instances ...).
            page = pages.get(path) or legacy.get("/" + "/".join(chain + [block_uuid]) + "/", "")
            result.append(
                SheetInstance(
                    path=child_path,
                    sheet_name=name,
                    file=file_name,
                    file_path=child,
                    page=page,
                    parent_path=path,
                )
            )
            key = _real_key(child)
            if key in ancestors or not child.is_file():
                continue
            try:
                child_text, _nl = read_text_preserve_newline(child)
            except (OSError, UnicodeDecodeError):
                continue
            walk(child, child_text, child_path, chain + [block_uuid], ancestors | {key})

    walk(root, text, f"/{root_uuid}", [], {_real_key(root)})
    return result
