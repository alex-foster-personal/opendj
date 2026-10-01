"""Where ruff 0.16.3 formats nothing: `# fmt: off` regions and `# fmt: skip` statements, read from tokens.

`scripts/format_proof.py` holds a comment or docstring to its exact text on the rows `formatter_disabled` returns,
because ruff changes nothing there, and allows ruff's normalization everywhere else. Every rule was measured on
ruff's own output in review 1q of issue #4456 and is pinned by tests/quality/test_format_proof_regions.py.

Requirements (mini-PRD):
  formatter_disabled  ✔︎ ✅ 🎯
    The rows ruff formats nothing on, and the rows of the pragmas it honors there, which it normalizes like any
    comment. A pragma is the whole comment after `#`, trimmed of spaces, tabs and form feeds only, and
    case-sensitive. An off pragma opens a region on its own line outside brackets, a trailing `# fmt: skip` holds
    its logical line, and a region whose first statement is simple and skipped is no region.
      [if] a pragma ruff ignores (trailing, in brackets, more text, capitals) opens a region [then ⛔️] test fails
      [if] a region outlives an honored `# fmt: on`, or ends at one ruff ignores [then ⛔️] test fails
      [if] a skip holds more or less than its logical line, or counts inside brackets [then ⛔️] test fails
      [if] a skipped simple first statement leaves its region open [then ⛔️] test fails

What could satisfy this without satisfying its intent: a model that marks every row fails every hand edit and
ruff's own output with it, and one that marks none passes both, so the tests hold ruff's real output on both sides
of each rule. Where the model cannot tell what ruff does, it keeps the region open, so the proof fails
rather than passing a hand edit.
"""

from __future__ import annotations

import tokenize
from dataclasses import dataclass


class CFG:
    # ruff trims only Python's whitespace around a pragma: `# fmt: off` then a no-break space is prose (review 1q).
    PRAGMA_WHITESPACE: str = " \t\x0c"
    # (prefix, command, what ruff does): the pragmas ruff 0.16.3 reads, each only alone in its comment or `#` part.
    PRAGMAS: tuple[tuple[str, str, str], ...] = (
        ("fmt:", "off", "off"),
        ("fmt:", "on", "on"),
        ("fmt:", "skip", "skip"),
        ("yapf:", "disable", "off"),
        ("yapf:", "enable", "on"),
    )
    # A statement starting with one of these is compound: a `# fmt: skip` on its first line never cancels a region.
    # `match` and `case` are soft keywords, so a plain statement starting with either keeps its region, the safe side.
    COMPOUND_STARTS: frozenset[str] = frozenset(
        {"if", "elif", "else", "for", "while", "with", "def", "class", "try", "except", "finally", "async", "@"}
        | {"match", "case"}
    )
    OPENING_BRACKETS: frozenset[str] = frozenset("([{")
    CLOSING_BRACKETS: frozenset[str] = frozenset(")]}")
    NOT_CODE: frozenset[int] = frozenset(
        {tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
    )


def _pragma(text: str) -> str | None:
    """What ruff reads a comment's text after its `#`, or one `#`-separated part of it, as: off, on, skip or None.
    Only the pragma alone counts, trimmed of Python's whitespace: `# fmt: off  # why` and `# FMT: OFF` are prose."""
    body = text.strip(CFG.PRAGMA_WHITESPACE)
    for prefix, command, kind in CFG.PRAGMAS:
        if body.startswith(prefix) and body[len(prefix) :].lstrip(CFG.PRAGMA_WHITESPACE) == command:
            return kind
    return None


def _is_skip(comment: str) -> bool:
    """ruff reads `# fmt: skip` alone or as one `#`-separated part of a comment: `# a  # fmt: skip  # noqa` counts."""
    return any(_pragma(part) == "skip" for part in comment[1:].split("#"))


def _bracket_step(tok: tokenize.TokenInfo) -> int:
    """+1 for an opening bracket, -1 for a closing one. Only an operator token: an f-string's text can be `(`."""
    if tok.type != tokenize.OP:
        return 0
    return (tok.string in CFG.OPENING_BRACKETS) - (tok.string in CFG.CLOSING_BRACKETS)


@dataclass
class _Region:
    """An open `# fmt: off` region, read one statement and one `# fmt: on` at a time."""

    opened: int  # its pragma's row
    level: int | None = None  # its block's column, once the statement after the pragma shows it
    nested: int | None = None  # the first column deeper than the block since the block's last statement
    closing: int | None = None  # an on pragma's row, until the next statement shows whether it ends the region
    lead: int | None = None  # the first statement's row when it is simple: a `# fmt: skip` there cancels the region

    def on_pragma(self, row: int, column: int) -> None:
        if self.closing is None and (self.nested is None or column < self.nested):
            self.closing = row

    def statement(self, row: int, column: int, first: str) -> int | None:
        """Read a statement's first token: the row the region ends before, or None while it stays open."""
        if self.level is None:
            self.lead = None if first in CFG.COMPOUND_STARTS else row
        self.level = column if self.level is None else self.level
        if self.closing is not None and column == self.level:
            return self.closing
        if column < self.level:
            return row
        if column == self.level:
            self.nested = None
        elif self.nested is None:
            self.nested = column
        self.closing = None
        return None

    def honored(self) -> list[int]:
        """The on pragma's row, when one ends the region: ruff normalizes it like any comment."""
        return [] if self.closing is None else [self.closing]


def formatter_disabled(tokens: list[tokenize.TokenInfo]) -> tuple[frozenset[int], frozenset[int]]:
    """(rows ruff 0.16.3 formats nothing on, rows of the pragmas it honors there, which it normalizes like any comment).

    A `# fmt: off` (or `# yapf: disable`) region opens at that pragma alone on its line, outside brackets, and holds
    the block of the statement after it. It closes before a statement dedented below that block, at the end of the
    file, or at a `# fmt: on` whose next statement is back at the block's column, unless the `# fmt: on` sits inside
    a nested block: after one, it must be indented less than that block's first statement. Columns are raw lengths,
    as ruff compares them, so a tab is one. A trailing `# fmt: skip` outside brackets, alone or as one `#` part of
    the comment, holds the other lines of its statement, or of its compound statement's header. Measured on ruff's
    output in review 1q. A region whose first statement is simple and carries such a skip is no region: ruff holds
    that statement only and formats on. Where the model and ruff differ, the model keeps the region open, so the
    proof fails rather than passing a hand edit."""
    rows: set[int] = set()
    pragmas: set[int] = set()
    region: _Region | None = None
    line_start: int | None = None  # the first row of the logical line being read
    depth = 0
    for tok in tokens:
        row = tok.start[0]
        if tok.type == tokenize.COMMENT and not depth:
            if line_start is not None and _is_skip(tok.string) and (region is None or region.lead == line_start):
                rows.update(range(line_start, row + 1))
                pragmas.add(row)
                region = None
            elif region is None and line_start is None and _pragma(tok.string[1:]) == "off":
                region = _Region(row)
                pragmas.add(row)
            elif region is not None and line_start is None and _pragma(tok.string[1:]) == "on":
                region.on_pragma(row, tok.start[1])
        elif tok.type == tokenize.NEWLINE:
            line_start = None
        elif tok.type == tokenize.ENDMARKER and region is not None:
            rows.update(range(region.opened, row + 1))
            pragmas.update(region.honored())
        elif tok.type not in CFG.NOT_CODE:
            depth += _bracket_step(tok)
            end = region.statement(row, tok.start[1], tok.string) if region is not None and line_start is None else None
            if region is not None and end is not None:
                rows.update(range(region.opened, end))
                pragmas.update(region.honored())
                region = None
            line_start = row if line_start is None else line_start
    return frozenset(rows), frozenset(pragmas)
