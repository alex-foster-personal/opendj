"""Exact per-line facts for one Python source, from ``tokenize`` and ``ast``.

The PERFBATCH-07 classifier reads these for BOTH sides of a diff (the file
at the merge base and at the head), so it never infers string or docstring
context from a three-line hunk window. A source that does not tokenize or
parse yields ``error`` and the caller fails closed.
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass, field

_STRING_TYPES = {tokenize.STRING} | {
    getattr(tokenize, name)
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
    if hasattr(tokenize, name)
}
_LAYOUT_TYPES = {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}


@dataclass(frozen=True)
class PythonFacts:
    """Line numbers are 1-based physical lines of the analyzed source."""

    docstring_lines: frozenset[int] = frozenset()
    string_lines: frozenset[int] = frozenset()  # any line a non-docstring string token touches
    comment_lines: frozenset[int] = frozenset()  # a comment and nothing else
    blank_lines: frozenset[int] = frozenset()
    first_token: dict[int, str] = field(default_factory=dict)  # first NAME/OP per code line
    error: str | None = None

    def is_code(self, line: int) -> bool:
        return (
            line in self.first_token
            and line not in self.string_lines
            and line not in self.docstring_lines
        )


def _docstring_spans(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and first.end_lineno is not None
        ):
            lines.update(range(first.lineno, first.end_lineno + 1))
    return lines


def analyze(source: str) -> PythonFacts:
    """Tokenize and parse ``source``; never raises, the failure lands in ``error``."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        tree = ast.parse(source)
    except (tokenize.TokenError, SyntaxError, ValueError) as exc:
        return PythonFacts(error=f"{type(exc).__name__}: {exc}")
    docstrings = _docstring_spans(tree)
    strings: set[int] = set()
    per_line: dict[int, list[tokenize.TokenInfo]] = {}
    for token in tokens:
        if token.type in _STRING_TYPES:
            strings.update(range(token.start[0], token.end[0] + 1))
        per_line.setdefault(token.start[0], []).append(token)
    strings -= docstrings
    comments: set[int] = set()
    blanks: set[int] = set()
    first: dict[int, str] = {}
    for line, line_tokens in per_line.items():
        if line in strings or line in docstrings:
            continue
        kinds = {token.type for token in line_tokens}
        if kinds <= _LAYOUT_TYPES:
            blanks.add(line)
            continue
        if kinds <= _LAYOUT_TYPES | {tokenize.COMMENT}:
            comments.add(line)
            continue
        for token in line_tokens:
            if token.type in (tokenize.NAME, tokenize.OP):
                first[line] = token.string
                break
    return PythonFacts(
        docstring_lines=frozenset(docstrings),
        string_lines=frozenset(strings),
        comment_lines=frozenset(comments),
        blank_lines=frozenset(blanks),
        first_token=first,
    )
