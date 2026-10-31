"""CLI option value renames for the PERFBATCH-07 classifier (issue #5639).

A stems pipeline file whose only behavioral change is a new spelling for one
of its own argparse ``choices`` values (``--stems-dest bifrost2`` becoming
``--stems-dest asset-store``) changes no throughput, latency or quality: the
same branches run on the same data, only the token that selects them is
spelled differently. This module proves that from both whole-file sources,
line by line, and fails closed on anything it cannot prove.

A changed line pair is a rename pair only when the added line is the removed
line, byte for byte, with some string tokens rewritten in exactly one of
these ways, for a rename ``old -> new``:

* whole literal: a plain literal whose value is ``old`` becomes the same
  literal (same prefix, same quote) holding ``new``, and is not one piece
  of an implicit concatenation (``"old" "-metrics"`` is another value);
* option mention: inside any string token, ``--<option> old`` (or
  ``--<option>=old``) becomes ``--<option> new``, for a ``--`` option whose
  ``choices`` hold ``old`` (a positional's name is never a mention);
* help text: inside the ``help=`` argument of an ``add_argument`` call, the
  word ``old`` becomes ``new`` (help text only reaches ``--help`` output).

The file-level checks then hold the rename to its definition: every rename
comes from at least one whole-literal rewrite, ``old`` is a ``choices``
member of an ``add_argument`` call at the merge base, the head has that
option's choices with ``new`` in the same place and nothing else changed,
the head holds no ``old`` string or UTF-8 bytes constant anywhere and no
``--<option> old`` mention inside a larger string constant (no partial rename),
the merge base holds no ``new`` string constant anywhere (no merge into an
existing value), and one ``old`` maps to one ``new`` and back.

``old`` as a bare word inside a larger string (``"preflight: bifrost2 asset
store reachable"``) is not a partial rename: no comparison or argv can select
the option value through it, and the real farm rename keeps such words because
they name the host, not the choice.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass, field

_SIMPLE_LITERAL = re.compile(r"^(?P<prefix>[rRuU]?)(?P<quote>['\"])(?P<value>[^'\"\\\n]*)(?P=quote)$")
_LAYOUT = {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}


def _constant_texts(tree: ast.AST) -> set[str]:
    """Text of string constants, plus UTF-8 bytes literals (``b"old"`` is still ``old``)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        value = node.value
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, bytes):
            try:
                found.add(value.decode("utf-8"))
            except UnicodeDecodeError:
                continue
    return found


def _byte_col(line: str, col: int) -> int:
    """``ast`` column offsets count UTF-8 bytes; ``tokenize`` columns count characters."""
    return len(line[:col].encode("utf-8"))


@dataclass
class RenameSide:
    """One whole-file source, read for the rename rule. ``error`` set means refuse."""

    lines: list[str] = field(default_factory=list)
    tokens: dict[int, list[tokenize.TokenInfo]] = field(default_factory=dict)
    multiline_rows: set[int] = field(default_factory=set)
    # Start positions of string tokens that are one piece of an implicit
    # concatenation (``"old" "-metrics"``): the piece is not the value.
    concatenated: set[tuple[int, int]] = field(default_factory=set)
    choices: dict[str, tuple[str, ...]] = field(default_factory=dict)
    help_spans: list[tuple[tuple[int, int], tuple[int, int]]] = field(default_factory=list)
    constants: set[str] = field(default_factory=set)
    error: str | None = None

    @classmethod
    def read(cls, source: bytes) -> RenameSide:
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
            text = source.decode(encoding)
            tokens = list(tokenize.generate_tokens(io.StringIO(text, newline="\n").readline))
            tree = ast.parse(text)
        except (tokenize.TokenError, SyntaxError, ValueError, LookupError) as exc:
            return cls(error=f"{type(exc).__name__}: {exc}")
        side = cls(lines=text.split("\n"))
        for token in tokens:
            if token.start[0] != token.end[0]:
                side.multiline_rows.update(range(token.start[0], token.end[0] + 1))
            if token.type not in _LAYOUT:
                side.tokens.setdefault(token.start[0], []).append(token)
        side.concatenated = _concatenated_pieces(tokens)
        side.constants = _constant_texts(tree)
        side._read_arguments(tree)
        return side

    def _read_arguments(self, tree: ast.Module) -> None:
        bindings = _module_string_tuples(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
            ):
                continue
            options = [arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
            for keyword in node.keywords:
                if keyword.arg == "help":
                    value = keyword.value
                    end = (value.end_lineno or value.lineno, value.end_col_offset or 0)
                    self.help_spans.append(((value.lineno, value.col_offset), end))
                elif keyword.arg == "choices" and options:
                    values = _string_tuple(keyword.value, bindings)
                    if values is not None:
                        # Every spelling of the option (``-d``, ``--dest``) holds them.
                        for option in options:
                            self.choices[option] = values

    def in_help(self, token: tokenize.TokenInfo) -> bool:
        line = self.lines[token.start[0] - 1]
        start = (token.start[0], _byte_col(line, token.start[1]))
        end = (token.end[0], _byte_col(line, token.end[1]))
        return any(lo <= start and end <= hi for lo, hi in self.help_spans)


def _concatenated_pieces(tokens: list[tokenize.TokenInfo]) -> set[tuple[int, int]]:
    """Starts of string tokens adjacent to another string token (comments and line breaks between)."""
    pieces: set[tuple[int, int]] = set()
    previous: tokenize.TokenInfo | None = None
    for token in tokens:
        if token.type in (tokenize.NL, tokenize.COMMENT):
            continue
        if token.type == tokenize.STRING and previous is not None and previous.type == tokenize.STRING:
            pieces.update((previous.start, token.start))
        previous = token
    return pieces


def _string_tuple(node: ast.expr, bindings: dict[str, tuple[str, ...] | None]) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.Tuple | ast.List) and node.elts:
        values = [elt.value for elt in node.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
        if len(values) == len(node.elts):
            return tuple(values)
    return None


def _module_string_tuples(tree: ast.Module) -> dict[str, tuple[str, ...] | None]:
    """Module-level names bound exactly once to a literal tuple or list of strings."""
    seen: dict[str, tuple[str, ...] | None] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            continue
        if isinstance(target, ast.Name):
            name = target.id
            seen[name] = None if name in seen else _string_tuple(value, {})
    return seen


@dataclass
class RenameLedger:
    """What one file's rename pairs claimed, checked once every hunk is read."""

    renames: dict[str, str] = field(default_factory=dict)
    # (removed token, added token, both inside help text): resolved at the end
    # against the literal renames, so a text rewrite never invents a rename.
    rewrites: list[tuple[str, str, bool, int]] = field(default_factory=list)
    conflict: str | None = None

    def claim(self, old: str, new: str) -> None:
        if self.renames.setdefault(old, new) != new:
            self.conflict = f"{old!a} renamed to two values"


def _rewrite(text: str, old: str, new: str, options: list[str], *, help_text: bool) -> str:
    """``text`` with ``--<option> old`` mentions renamed, and in help text the word ``old``."""
    for option in (name for name in options if name.startswith("--")):
        mention = re.compile(rf"(?<![\w-])({re.escape(option)}[ =]){re.escape(old)}(?![\w-])")
        text = mention.sub(lambda match: match.group(1) + new, text)
    if help_text:
        text = re.sub(rf"(?<![\w-]){re.escape(old)}(?![\w-])", new, text)
    return text


def _pair_token(
    before: tokenize.TokenInfo,
    after: tokenize.TokenInfo,
    sides: tuple[RenameSide, RenameSide],
    ledger: RenameLedger,
) -> None:
    old_match = _SIMPLE_LITERAL.match(before.string)
    new_match = _SIMPLE_LITERAL.match(after.string)
    if (
        old_match
        and new_match
        and old_match["prefix"] == new_match["prefix"]
        and old_match["quote"] == new_match["quote"]
        and new_match["value"]
        and _options_holding(sides[0], old_match["value"])
        and before.start not in sides[0].concatenated
        and after.start not in sides[1].concatenated
    ):
        ledger.claim(old_match["value"], new_match["value"])
        return
    if any(set(_string_prefix(token.string)) & {"f", "b"} for token in (before, after)):
        # An f-string holds code in its braces and a bytes literal is not text:
        # neither is rewritten as prose.
        ledger.conflict = f"f-string or bytes literal changed: {before.string!a}"
        return
    base, head = sides
    help_text = base.in_help(before) and head.in_help(after)
    ledger.rewrites.append((before.string, after.string, help_text, after.start[0]))


def _string_prefix(literal: str) -> str:
    """The lowercased prefix letters of a string token (``rb``, ``f``, or empty)."""
    return literal[: len(literal) - len(literal.lstrip("rRuUbBfF"))].lower()


def rename_line_pair(
    old_row: int,
    new_row: int,
    removed: str,
    added: str,
    sides: tuple[RenameSide, RenameSide],
    ledger: RenameLedger,
) -> bool:
    """True when ``added`` is ``removed`` with only rename rewrites of its string tokens."""
    base, head = sides
    if old_row in base.multiline_rows or new_row in head.multiline_rows:
        return False
    if old_row > len(base.lines) or new_row > len(head.lines):
        return False
    if base.lines[old_row - 1] != removed or head.lines[new_row - 1] != added:
        return False
    before = base.tokens.get(old_row, [])
    after = head.tokens.get(new_row, [])
    if not before or len(before) != len(after):
        return False
    expected, cursor, changed = [], 0, False
    for left, right in zip(before, after, strict=True):
        if left.type != right.type:
            return False
        if left.string == right.string:
            continue
        if left.type != tokenize.STRING:
            return False
        _pair_token(left, right, sides, ledger)
        expected.append(removed[cursor : left.start[1]] + right.string)
        cursor, changed = left.end[1], True
    expected.append(removed[cursor:])
    return changed and "".join(expected) == added


@dataclass(frozen=True)
class RenameRefusal:
    """Why a file is not one consistent rename; ``line`` is the head line at fault, when one is."""

    reason: str
    line: int | None = None


def ledger_refusal(ledger: RenameLedger, sides: tuple[RenameSide, RenameSide]) -> RenameRefusal | None:
    """Why the claimed renames are not one consistent CLI choice-value rename, or None."""
    base, head = sides
    if ledger.conflict:
        return RenameRefusal(f"cli value rename: {ledger.conflict}")
    renames = ledger.renames
    if len(set(renames.values())) != len(renames):
        return RenameRefusal("cli value rename: two values renamed to one")
    for before, after, help_text, line in ledger.rewrites:
        if not any(
            _rewrite(before, old, new, _options_holding(base, old), help_text=help_text) == after
            for old, new in renames.items()
        ):
            return RenameRefusal(
                f"cli value rename: string {before!a} -> {after!a} is not a rename of a choices value", line
            )
    for old, new in sorted(renames.items()):
        for option in _options_holding(base, old):
            want = tuple(new if value == old else value for value in base.choices[option])
            if head.choices.get(option) != want:
                return RenameRefusal(f"cli value rename: {option} choices are not {old!a} renamed to {new!a}")
        if old in head.constants:
            return RenameRefusal(f"cli value rename: {old!a} still appears in the head (partial rename)")
        if new in base.constants:
            return RenameRefusal(f"cli value rename: {new!a} already exists at the merge base")
        mention = _option_mention(head.constants, old, _options_holding(base, old))
        if mention is not None:
            return RenameRefusal(f"cli value rename: {mention!a} still appears in a head string (partial rename)")
    return None


def _options_holding(side: RenameSide, value: str) -> list[str]:
    return [option for option, values in side.choices.items() if value in values]


def _option_mention(texts: set[str], old: str, options: list[str]) -> str | None:
    """The first ``--<option> old`` (or ``--<option>=old``) mention inside any of ``texts``, or None."""
    for option in sorted(name for name in options if name.startswith("--")):
        mention = re.compile(rf"(?<![\w-]){re.escape(option)}([ =]){re.escape(old)}(?![\w-])")
        for text in sorted(texts):
            found = mention.search(text)
            if found:
                return f"{option}{found.group(1)}{old}"
    return None
