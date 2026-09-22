"""Triple-quoted string tracking for the PERFBATCH-07 hunk classifier.

Decides, per hunk side, which lines lie inside a docstring (benign) and
which lie inside any other triple-quoted region (string content: never
benign, never eligible for widening or import-guard pairing). Works on the
hunk window alone; see docs/perf/perfbatch-gates.md for the residuals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.perf.perfbatch_hunks import Hunk

_TRAILING_COMMENT = r"\s*(?:#.*)?$"
_BLOCK_HEADER_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+\w+.*:" + _TRAILING_COMMENT)
_SIGNATURE_CLOSE_RE = re.compile(r"^\s*\)\s*(?:->\s*.+?)?\s*:" + _TRAILING_COMMENT)


def _side_lines(hunk: Hunk, tag: str) -> list[tuple[int, str]]:
    """(hunk index, text) for one side of a hunk: context plus that side's tag."""
    return [(i, text) for i, (t, text) in enumerate(hunk.lines) if t in (" ", tag)]


def _unescaped_find(text: str, delimiter: str, start: int = 0) -> int:
    """Index of the next ``delimiter`` not preceded by an odd run of backslashes, else -1.

    A backslash right before the triple quote escapes it, so that quote keeps
    the string open (Codex P1 on #3804); an escaped quote never terminates a
    literal, raw or not. An even run of backslashes is itself escaped, and
    the quote after it is a real delimiter.
    """
    position = text.find(delimiter, start)
    while position != -1:
        backslashes = 0
        while position - backslashes - 1 >= 0 and text[position - backslashes - 1] == "\\":
            backslashes += 1
        if backslashes % 2 == 0:
            return position
        position = text.find(delimiter, position + 1)
    return -1


def _first_delimiter(text: str, start: int = 0) -> tuple[int, str] | None:
    """Earliest unescaped triple quote of either kind at or after ``start``."""
    found = [
        (position, delimiter)
        for delimiter in ('"""', "'''")
        if (position := _unescaped_find(text, delimiter, start)) != -1
    ]
    return min(found) if found else None


@dataclass
class _StringWalk:
    """Per-side triple-quoted string tracker: docstring lines vs other string content."""

    docstring: set[int] = field(default_factory=set)
    other: set[int] = field(default_factory=set)
    kind: str | None = None  # "doc" | "other" while inside a multi-line string
    delimiter: str = ""
    block: list[int] = field(default_factory=list)
    prev_code: str | None = None

    def open(self, kind: str, delimiter: str, index: int) -> None:
        self.kind, self.delimiter, self.block = kind, delimiter, [index]

    def close(self, index: int, tail: str) -> None:
        self.block.append(index)
        bare = not tail.strip() or tail.strip().startswith("#")
        # A docstring whose closer is followed by code (``""".format(x)``) is
        # an expression, not a bare statement: its lines are string content.
        target = self.docstring if self.kind == "doc" and bare else self.other
        target.update(self.block)
        self.kind, self.block = None, []

    def abandon(self) -> None:
        """Closer beyond the hunk window: nothing inside can be proven benign."""
        self.other.update(self.block)
        self.kind, self.block = None, []


def _string_regions(hunk: Hunk, tag: str, start_line: int) -> _StringWalk:
    """Classify each line on side ``tag`` that lies inside a triple-quoted string.

    A string is a DOCSTRING only when it opens at the start of a line whose
    nearest preceding code line on that side is a ``def``/``class`` header (or
    a multi-line signature close), or when nothing but comments and blank
    lines precede it from line 1 of the file, and its closer is visible with
    nothing but a comment after it. Every other triple-quoted region (a
    prompt, SQL, a shell template, an unclosed block) is OTHER string content:
    never benign, never eligible for widening or import-guard pairing.
    """
    walk = _StringWalk()
    at_file_top = start_line == 1
    for index, text in _side_lines(hunk, tag):
        if walk.kind is None:
            stripped = text.strip()
            if not stripped or stripped.startswith("#"):
                continue
        _scan_line(walk, index, text, at_file_top)
    if walk.kind is not None:
        walk.abandon()
    return walk


def _docstring_position(walk: _StringWalk, at_file_top: bool) -> bool:
    """A string opening here is a docstring only after a header, or at the top of the file."""
    if walk.prev_code is None:
        return at_file_top
    return bool(_BLOCK_HEADER_RE.match(walk.prev_code) or _SIGNATURE_CLOSE_RE.match(walk.prev_code))


def _scan_line(walk: _StringWalk, index: int, text: str, at_file_top: bool) -> None:
    """Consume ONE line end to end: close an open region, then every string the rest opens.

    Scanning always resumes after a closer, so a region that closes and
    another that opens on the same line (a closer directly followed by an
    opener, Codex P1 at 939ffbc95) leaves the second region tracked as
    string content. Only the first string on a line, opening at its start,
    can be a docstring.
    """
    position = 0
    while True:
        if walk.kind is not None:
            closer = _unescaped_find(text, walk.delimiter, position)
            if closer == -1:
                if not walk.block or walk.block[-1] != index:
                    walk.block.append(index)
                return
            walk.close(index, text[closer + 3 :])
            walk.prev_code = text
            position = closer + 3
            continue
        opener = _first_delimiter(text, position)
        if opener is None:
            walk.prev_code = text
            return
        start, delimiter = opener
        is_doc = (
            position == 0
            and text[:start].strip() in ("", "r", "R")
            and _docstring_position(walk, at_file_top)
        )
        closer = _unescaped_find(text, delimiter, start + 3)
        if closer == -1:
            walk.open("doc" if is_doc else "other", delimiter, index)
            walk.prev_code = text
            return
        after = text[closer + 3 :].strip()
        if is_doc and (not after or after.startswith("#")):
            walk.docstring.add(index)
        walk.prev_code = text
        position = closer + 3
