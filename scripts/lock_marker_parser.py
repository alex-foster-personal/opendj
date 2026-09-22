"""PEP 508 marker text to a small AST, shared by the lock gate's comparators.

Split out of scripts/lock_marker_semantics.py (the evaluator) so each stays inside
the 600-line file-size ratchet. Stdlib only. The AST: `("true",)`, `("or", a, b)`,
`("and", a, b)` and `("cmp", lhs, op, rhs)` with each side a `("word", name)` or a
`("str", quoted_literal)` token; a marker `uv.lock` records is always parseable
here, and one it would not record raises Unknown rather than guessing.
"""

from __future__ import annotations

import re


class Unknown(Exception):
    """The check could not measure; the caller must report UNKNOWN, not a verdict."""


_TOKEN_RE = re.compile(
    r"\s*(?:(?P<lp>\()|(?P<rp>\))|(?P<str>'[^']*'|\"[^\"]*\")|(?P<word>[A-Za-z_][A-Za-z0-9_.]*)"
    r"|(?P<op>===|==|!=|<=|>=|<|>|~=))"
)


def tokenize_marker(text: str) -> list[tuple[str, str]]:
    """PEP 508 marker tokens. A string literal may use either quote form and keeps
    its contents verbatim (`"posix's"` is a valid literal uv records as is); its
    token text is re-quoted canonically, single quotes unless the value holds one,
    so the two spellings of one value compare equal downstream."""
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        match = _TOKEN_RE.match(text, pos)
        if match is None or match.end() == pos:
            raise Unknown(f"unparseable marker: {text!r}")
        pos = match.end()
        kind = match.lastgroup or ""
        token = match.group(kind)
        if kind == "str":
            token = quote_literal(token[1:-1])
        out.append((kind, token))
    return out


def quote_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    raise Unknown(f"a marker literal with both quote characters is not representable: {value!r}")


class _MarkerParser:
    """Recursive descent over PEP 508's marker grammar: or > and > atom | (expr)."""

    def __init__(self, text: str) -> None:
        self.toks = tokenize_marker(text)
        self.i = 0
        self.text = text

    def _peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _take(self) -> tuple[str, str]:
        tok = self._peek()
        if tok is None:
            raise Unknown(f"marker ends early: {self.text!r}")
        self.i += 1
        return tok

    def parse(self) -> tuple:
        node = self._or()
        if self._peek() is not None:
            raise Unknown(f"trailing tokens in marker: {self.text!r}")
        return node

    def _or(self) -> tuple:
        node = self._and()
        while self._peek() == ("word", "or"):
            self._take()
            node = ("or", node, self._and())
        return node

    def _and(self) -> tuple:
        node = self._atom()
        while self._peek() == ("word", "and"):
            self._take()
            node = ("and", node, self._atom())
        return node

    def _atom(self) -> tuple:
        tok = self._take()
        if tok[0] == "lp":
            node = self._or()
            if self._take()[0] != "rp":
                raise Unknown(f"unbalanced parentheses in marker: {self.text!r}")
            return node
        lhs = tok
        op_tok = self._take()
        if op_tok == ("word", "not"):
            if self._take() != ("word", "in"):
                raise Unknown(f"'not' without 'in' in marker: {self.text!r}")
            op = "not in"
        elif op_tok == ("word", "in"):
            op = "in"
        elif op_tok[0] == "op":
            op = op_tok[1]
        else:
            raise Unknown(f"expected an operator in marker: {self.text!r}")
        rhs = self._take()
        for side in (lhs, rhs):
            if side[0] not in ("str", "word"):
                raise Unknown(f"expected a variable or string in marker: {self.text!r}")
        return ("cmp", lhs, op, rhs)


# ----- evaluation over a probe grid ---------------------------------------------
