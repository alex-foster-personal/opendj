#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Re-check every docs/moc/*.md citation against the live tree.

A MOC (map of content, see .agents/skills/area-moc/SKILL.md) cites its claims
two ways, both backtick-delimited so they are trivial to find in prose:

  1. Path citation: a backtick path, optionally with a line or line range,
     e.g. `` `apps/engine_core/lock.py` `` or `` `apps/engine_core/app.py:381-386` ``.
     Checked for existence only (and that the line/range is in bounds).
  2. Threshold citation: a backtick snippet immediately followed by a
     parenthesized backtick `path:line` pointer, e.g.
     `` `HEARTBEAT_TTL_S: float = 120.0` (`apps/engine_core/lock.py:22`) ``.
     Checked for existence, line-in-bounds, AND that the cited line's
     stripped text still CONTAINS the quoted snippet.

This intentionally only understands repo-relative paths. A citation to a path
outside the repo tree (nucbox `~/jobs/...`, another repo) never matches
PATH_RE (no leading `~`), so it is silently not checked -- those sources are
supposed to be marked "unread" or "not verified here" in the MOC prose itself,
per the area-moc skill's sourcing rule, rather than cited as if this script
covers them.

A repository-ROOT file has no `/` to mark it as a path, and a bare backtick
name is ambiguous: `` `state.db` `` or `` `app.py` `` in MOC prose is a
basename, not a root citation (measured Mon 21 Sep 2026: treating every
extension-bearing bare name as a root path reported 130 false MISSING rows
across ten MOCs). So a root-level name is a citation only when it is
unambiguous (ROOT_PATH_RE plus `_is_root_citation`):

  - a letter-initial name with a dot extension AND a `:line` or `:from-to`
    pointer, e.g. `` `AGENTS.md:56-99` `` or `` `CLAUDE.md:23-29` ``, or
  - one of the extensionless build files in ROOT_BARE_FILES (`justfile`,
    `Makefile`), with or without a line pointer.

`` `127.0.0.1:8585` `` (digit-initial) and `` `localhost:8585` `` (no
extension) are host:port literals, not citations, and do not match. A bare
`` `AGENTS.md` `` or `` `reqs.json` `` stays prose: cite a line to have it
checked.

Exit code is nonzero iff any citation is MISSING or DRIFTED. A MOC with zero
citations is not an error (nothing to check), but is called out in the table
so an empty MOC does not read as a silent pass.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

THRESHOLD_RE = re.compile(
    r"`(?P<snippet>[^`\n]+)`\s*\(`(?P<path>[A-Za-z0-9_][A-Za-z0-9_.\-/]*):(?P<line>\d+)`\)"
)
PATH_RE = re.compile(
    r"`(?P<path>[A-Za-z0-9_][A-Za-z0-9_.\-/]*/[A-Za-z0-9_.\-]+)"
    r"(?::(?P<line1>\d+)(?:-(?P<line2>\d+))?)?`"
)
# Root-level candidates: no `/` at all. Every match is then filtered through
# `_is_root_citation` -- the regex alone would also match a bare basename.
ROOT_PATH_RE = re.compile(
    r"`(?P<path>[A-Za-z_][A-Za-z0-9_.\-]*)(?::(?P<line1>\d+)(?:-(?P<line2>\d+))?)?`"
)
ROOT_BARE_FILES = frozenset({"justfile", "Makefile"})

OK = "OK"
MISSING = "MISSING"
DRIFT = "DRIFT"


@dataclass
class Row:
    moc: str
    kind: str
    citation: str
    status: str
    detail: str


def _read_lines(repo_root: Path, rel_path: str) -> list[str] | None:
    full = repo_root / rel_path
    if not full.is_file():
        return None
    return full.read_text(encoding="utf-8", errors="replace").splitlines()


def _check_line_in_bounds(lines: list[str], line_no: int) -> bool:
    return 1 <= line_no <= len(lines)


def check_threshold(repo_root: Path, moc_name: str, snippet: str, path: str, line_no: int) -> Row:
    citation = f"`{snippet}` (`{path}:{line_no}`)"
    lines = _read_lines(repo_root, path)
    if lines is None:
        return Row(moc_name, "threshold", citation, MISSING, f"{path} does not exist")
    if not _check_line_in_bounds(lines, line_no):
        return Row(
            moc_name,
            "threshold",
            citation,
            MISSING,
            f"{path} has {len(lines)} lines, cited line {line_no} is out of bounds",
        )
    actual = lines[line_no - 1].strip()
    if snippet.strip() not in actual:
        return Row(
            moc_name,
            "threshold",
            citation,
            DRIFT,
            f"{path}:{line_no} now reads {actual!r}, does not contain {snippet!r}",
        )
    return Row(moc_name, "threshold", citation, OK, "")


def check_path(
    repo_root: Path, moc_name: str, path: str, line1: str | None, line2: str | None
) -> Row:
    citation = f"`{path}" + (f":{line1}" + (f"-{line2}" if line2 else "") if line1 else "") + "`"
    full = repo_root / path
    if not full.exists():
        return Row(moc_name, "path", citation, MISSING, f"{path} does not exist")
    if line1 is None:
        return Row(moc_name, "path", citation, OK, "")
    lines = _read_lines(repo_root, path)
    if lines is None:
        return Row(moc_name, "path", citation, MISSING, f"{path} is not a readable file")
    hi = int(line2) if line2 else int(line1)
    if not _check_line_in_bounds(lines, int(line1)) or not _check_line_in_bounds(lines, hi):
        return Row(
            moc_name,
            "path",
            citation,
            MISSING,
            f"{path} has {len(lines)} lines, cited range {line1}-{line2 or line1} is out of bounds",
        )
    return Row(moc_name, "path", citation, OK, "")


def _is_root_citation(m: re.Match[str]) -> bool:
    """A root-level backtick name counts as a citation only when unambiguous.

    A dotted, letter-initial name with a line pointer (`AGENTS.md:56-99`), or
    one of ROOT_BARE_FILES (`justfile`, `Makefile`) with or without one. A bare
    dotted name (`state.db`), a digit-initial host (`127.0.0.1:8585`), or an
    extensionless word with a port (`localhost:8585`) is prose, not a citation.
    """
    path = m.group("path")
    if path in ROOT_BARE_FILES:
        return True
    return m.group("line1") is not None and "." in path


def scan_moc(repo_root: Path, moc_path: Path) -> list[Row]:
    moc_name = moc_path.name
    text = moc_path.read_text(encoding="utf-8")
    rows: list[Row] = []

    threshold_spans: list[tuple[int, int]] = []
    for m in THRESHOLD_RE.finditer(text):
        threshold_spans.append(m.span())
        rows.append(
            check_threshold(
                repo_root, moc_name, m.group("snippet"), m.group("path"), int(m.group("line"))
            )
        )

    def _in_threshold_span(pos: int) -> bool:
        return any(start <= pos < end for start, end in threshold_spans)

    seen_paths: set[tuple[str, str | None, str | None]] = set()
    path_matches = list(PATH_RE.finditer(text))
    path_matches.extend(m for m in ROOT_PATH_RE.finditer(text) if _is_root_citation(m))
    for m in path_matches:
        if _in_threshold_span(m.start()):
            continue
        key = (m.group("path"), m.group("line1"), m.group("line2"))
        if key in seen_paths:
            continue
        seen_paths.add(key)
        rows.append(
            check_path(repo_root, moc_name, m.group("path"), m.group("line1"), m.group("line2"))
        )

    return rows


def render_table(rows: list[Row]) -> str:
    if not rows:
        return "(no citations found)"
    widths = {
        "moc": max(len("MOC"), *(len(r.moc) for r in rows)),
        "kind": max(len("kind"), *(len(r.kind) for r in rows)),
        "citation": max(len("citation"), *(len(r.citation) for r in rows)),
        "status": max(len("status"), *(len(r.status) for r in rows)),
    }
    lines = [
        f"{'MOC':<{widths['moc']}}  {'kind':<{widths['kind']}}  "
        f"{'citation':<{widths['citation']}}  {'status':<{widths['status']}}  detail"
    ]
    lines.extend(
        f"{r.moc:<{widths['moc']}}  {r.kind:<{widths['kind']}}  "
        f"{r.citation:<{widths['citation']}}  {r.status:<{widths['status']}}  {r.detail}"
        for r in rows
    )
    return "\n".join(lines)


def run(repo_root: Path, moc_dir: Path) -> tuple[list[Row], int]:
    moc_files = sorted(moc_dir.glob("*.md"))
    all_rows: list[Row] = []
    for moc_path in moc_files:
        all_rows.extend(scan_moc(repo_root, moc_path))
    drift_count = sum(1 for r in all_rows if r.status != OK)
    return all_rows, drift_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_repo_root = Path(__file__).resolve().parent.parent
    parser.add_argument("--repo-root", type=Path, default=default_repo_root)
    parser.add_argument("--moc-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    moc_dir: Path = (args.moc_dir or (repo_root / "docs" / "moc")).resolve()

    if not moc_dir.is_dir():
        print(f"[moc-refresh] ERROR no such MOC directory: {moc_dir}", file=sys.stderr)
        return 2

    rows, drift_count = run(repo_root, moc_dir)
    print(render_table(rows))
    if drift_count:
        print(f"\n[moc-refresh] FAIL {drift_count} of {len(rows)} citations are MISSING or DRIFTED")
        return 1
    print(f"\n[moc-refresh] OK {len(rows)} citations checked, 0 drift")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
