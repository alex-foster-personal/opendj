"""Prove a formatting commit is format-only, and keep .git-blame-ignore-revs honest.

Issue #4456 lands a whole-tree formatter pass in parts, and blame is told to skip
each part's commit. That is only safe if the commit really changed nothing but
layout, and only works if the SHA blame is told about is the one main contains.

    python -m scripts.format_proof prove --base <rev> [--head <rev>]
    python -m scripts.format_proof ignore-revs [--file .git-blame-ignore-revs]

Requirements (mini-PRD):
  prove  ✔︎
    Every .py file the range modifies decodes by its own cookie (so a cookie moved in
    or out of reach is judged by what the bytes then MEAN), parses on both sides and
    has an equal AST, each docstring's value (as for any string) untouched or exactly ruff's re-layout of it:
    each line's trailing whitespace stripped, the first line's leading whitespace too,
    and the rest re-indented to the statement with their relative indent kept (Black's
    safety check strips every line, which would pass a change to a doctest's relative
    indentation; inspect.cleandoc expands every tab and drops blank first lines, which
    ruff keeps). Every comment (prose, `# type:`, noqa, nosec, pragma, fmt: ...)
    keeps its text, its order, how many AST nodes open and close before it, how many
    names, keywords, numbers and operators precede it, which piece of an implicitly
    concatenated string it follows, and whether code precedes it on its line.
    Only its line may change, and its text only to exactly what ruff makes of it:
    trailing whitespace stripped, one space added after `#`. A shebang stays on byte 0, or stays off it.
      [if] a changed file's value, name or structure differs [then ⛔️] exit 1 naming it
      [if] a docstring's relative indentation changes [then ⛔️] exit 1
      [if] a docstring's tab between words, form feed, blank first line or escaped whitespace changes [then ⛔️] exit 1
      [if] a docstring gains whitespace ruff would strip, or leaves its statement's indent [then ⛔️] exit 1
      [if] ruff re-lays a docstring with an escape, an indent not tabs then spaces, or a `"` by a quote [then ⛔️] exit 1
      [if] a comment is added, removed, reworded, reordered, or moved past a node or fixed token [then ⛔️] exit 1
      [if] ruff moves a trailing operator past an end-of-line comment [then ⛔️] exit 1 (no count tells it from a move)
      [if] an end-of-line comment moves onto its own line, e.g. a block header's pragma into the body [then ⛔️] exit 1
      [if] a shebang moves off byte 0 or onto it [then ⛔️] exit 1
      [if] the range adds, deletes or renames a file, or changes a file's mode [then ⛔️] exit 1
      [if] the range also modifies a file that is not .py [then ⛔️] exit 2 UNKNOWN, since no AST can prove it
      [if] a changed .py path is a symlink or a submodule, not a regular file [then ⛔️] exit 2 UNKNOWN
      [if] a file does not decode or parse, or no .py file changed [then ⛔️] exit 2 UNKNOWN, never a pass
  ignore-revs  ✔︎
    Every listed SHA names a commit object itself, is an ancestor of HEAD, has a style(format): subject, and has
    one parent against which `prove` passes: the subject is a claim, the proof is what blame may rely on.
      [if] a listed commit changes code or a comment against its parent [then ⛔️] exit 1
      [if] a listed commit is a merge or a root commit [then ⛔️] exit 1
      [if] a listed commit also changes a file prove cannot read [then ⛔️] exit 2 UNKNOWN
      [if] a line is not a full 40-hex SHA or a comment [then ⛔️] exit 1
      [if] a SHA names a tag or other object that only peels to a commit [then ⛔️] exit 1
      [if] a SHA is not an ancestor of HEAD (squash or rebase merge) [then ⛔️] exit 1
      [if] the clone is shallow and cannot see a SHA or its parent [then ⛔️] exit 2 UNKNOWN

What could satisfy this without satisfying its intent: a normalizer that strips
whitespace from EVERY string would pass a real edit to a string value, so only
docstring positions are re-laid (tests/quality/test_format_proof_docstrings.py pins
that), and one that strips BOTH sides would pass whitespace ADDED to a docstring, so
each head docstring must be its base or ruff's exact output for it.
A proof over zero files would read as success, so it exits 2 instead. A style(format):
subject is only a claim, so ignore-revs proves every listed commit again. A comment's
LINE is not compared, because every re-wrap above it moves it; its place in the tree
is, counted in nodes and in the tokens ruff never adds or drops. So a noqa or type-ignore
moved to another statement or argument fails even when a count-based ratchet would net
to zero, and so does a whole-module type-ignore moved below the module's first line.
Whether a pragma still covers its finding after a re-wrap inside one statement is not
an AST property: the part's ruff and mypy ratchet runs decide that. Reading both sides
as utf-8 would pass a cookie moved out of reach, so each side decodes as Python would.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import importlib.util
import io
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


# A comment's place: AST nodes opened and closed before it, fixed tokens before it, own-line, string piece.
Place = tuple[int, int, int, bool, int]


class CFG:
    IGNORE_REVS_FILE: str = ".git-blame-ignore-revs"
    FORMAT_SUBJECT_PREFIX: str = "style(format):"
    SHA_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{40}$")
    # A fixed-length tuple type, so mypy narrows isinstance() to nodes that have .body.
    # Nodes whose last token is their own. Every other node ends on its last child or on a redundant `)` that ruff
    # may drop, as `x = (\n    1  # c\n)` becomes `x = 1  # c`, so its end is not a place a comment can be held to.
    POSITIONED_NODES: tuple[
        type[ast.stmt],
        type[ast.expr],
        type[ast.excepthandler],
        type[ast.arg],
        type[ast.keyword],
        type[ast.alias],
        type[ast.pattern],
    ] = (ast.stmt, ast.expr, ast.excepthandler, ast.arg, ast.keyword, ast.alias, ast.pattern)
    OWN_END_NODES: tuple[type[ast.expr], ...] = (
        ast.Name,
        ast.Constant,
        ast.JoinedStr,
        ast.Attribute,
        ast.Call,
        ast.Subscript,
        ast.List,
        ast.Dict,
        ast.Set,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
    )
    # A symlink's blob is its target and a gitlink's is a commit, so only these entries hold Python source.
    REGULAR_FILE_MODES: frozenset[str] = frozenset({"100644", "100755"})
    # An f-string or t-string is one string piece from its START token to its END token (Python 3.12 and later).
    PIECE_OPENERS: frozenset[int] = frozenset(
        number for number, name in tokenize.tok_name.items() if name in {"FSTRING_START", "TSTRING_START"}
    )
    PIECE_CLOSERS: frozenset[int] = frozenset(
        number for number, name in tokenize.tok_name.items() if name in {"FSTRING_END", "TSTRING_END"}
    )
    # The only operator tokens ruff adds or drops: parentheses, trailing commas, and the `;` it splits statements at.
    MOVABLE_OPERATORS: frozenset[str] = frozenset({"(", ")", ",", ";"})
    # ruff's whitespace, in docstrings and comments, is Unicode White_Space (Rust's char::is_whitespace): Python's
    # isspace() less U+001C to U+001F. Measured per character against ruff 0.16.3 in reviews 1l and 1m.
    RUFF_WHITESPACE: str = (
        "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
        "\u2028\u2029\u202f\u205f\u3000"
    )
    # A tab in a docstring's indent is this many columns to ruff, whatever its configured indent width.
    TAB_COLUMNS: int = 8
    DOCSTRING_OWNERS: tuple[type[ast.Module], type[ast.ClassDef], type[ast.FunctionDef], type[ast.AsyncFunctionDef]] = (
        ast.Module,
        ast.ClassDef,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
    )


@dataclass
class Result:
    exit_code: int
    files_checked: int = 0
    docstring_normalized: int = 0
    lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Docstring:
    text: str
    indent: str | None  # its statement's indent, None when code precedes it on its line
    plain: bool  # the source between its quotes IS its text


# ----- git helpers ----------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def _changed_entries(repo: Path, base: str, head: str) -> list[tuple[str, str, str, str]]:
    """(status, path, old mode, new mode) per changed file; a chmod-only change is status M."""
    out = _git(repo, "diff", "--raw", "--no-renames", "--no-abbrev", base, head).stdout
    entries: list[tuple[str, str, str, str]] = []
    for meta, path in (ln.split("\t", 1) for ln in out.splitlines() if ln):
        old_mode, new_mode, _, _, status = meta.lstrip(":").split()
        entries.append((status, path, old_mode, new_mode))
    return entries


def _blob(repo: Path, rev: str, path: str) -> bytes:
    """Raw bytes: only Python's own decode knows which of a file's lines its cookie may sit on."""
    return subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True, check=True).stdout


# ----- AST comparison -------------------------------------------------------


def _split_indent(line: str) -> tuple[str, str]:
    body = line.lstrip(CFG.RUFF_WHITESPACE)
    return line[: len(line) - len(body)], body


def _indent_columns(indent: str) -> int | None:
    """ruff's width of a docstring line's indent when it is tabs then spaces: 8 columns a tab and 1 a space. None
    otherwise: ruff strips a no-break space as indent but counts it as nothing, and measures ` \t ` as 10 columns."""
    spaces = indent.lstrip("\t")
    if spaces.strip(" "):
        return None
    return (len(indent) - len(spaces)) * CFG.TAB_COLUMNS + len(spaces)


def _ruff_reindent(rest: list[str], indent: str) -> list[str] | None:
    """A docstring's lines after its first, trailing whitespace already stripped, re-indented as ruff does: to the
    statement's `indent` plus each line's indent beyond the least-indented, a blank line emptied, and a blank last
    line holding the closing quotes at `indent`. None when an indent has no column count ruff agrees with."""
    split = [_split_indent(line) for line in rest]
    widths = [width for width in (_indent_columns(line_indent) for line_indent, _ in split) if width is not None]
    if len(widths) != len(split):
        return None
    common = min(width for width, (_, body) in zip(widths, split, strict=True) if body)
    pairs = zip(widths, split, strict=True)
    relaid = [indent + " " * (width - common) + body if body else "" for width, (_, body) in pairs]
    return [*relaid[:-1], relaid[-1] or indent]


def _ruff_docstring(text: str, indent: str) -> str | None:
    """What ruff 0.16.3 makes of a plain docstring whose statement is indented by `indent`, and nothing looser: every
    line loses its trailing whitespace, the first its leading whitespace too, and the rest are re-indented. A `"` just
    inside the opening quotes gets one space, a docstring blank after its first line collapses onto it, and a blank one
    keeps one space. Lines split only at newlines, since a tab between words, a form feed or a blank first line is
    text `__doc__` carries and ruff keeps. None where ruff's output is not modeled, so the docstring's text must stay
    as it was: an indent that is not tabs then spaces, a `"` or a backslash just inside the closing quotes, where ruff
    pads or keeps its quotes, and three double quotes inside, which keep single quotes. Its text, not its spelling:
    the AST holds a string's value, and a same-value respelling is no change to it, docstring or not."""
    lines = [line.rstrip(CFG.RUFF_WHITESPACE) for line in text.split("\n")]
    first = lines[0].lstrip(CFG.RUFF_WHITESPACE)
    if first.startswith('"'):
        first = " " + first  # ruff's pad, so the opening quotes do not run into the text
    if any(lines[1:]):
        rest = _ruff_reindent(lines[1:], indent)
        doc = None if rest is None else "\n".join([first, *rest])
    elif first or not text:
        doc = first
    else:
        doc = " "  # ruff keeps one space in a blank docstring, and none in an empty one
    if doc is None or doc.endswith(('"', "\\")) or '"""' in text:
        return None
    return doc


def _is_plain_literal(source: str, node: ast.Constant) -> bool:
    """True when the source between the literal's quotes IS its value: no escape, line continuation or second piece.
    Otherwise `__doc__` cannot tell an escaped tab, which ruff keeps, from a literal one, which it may strip."""
    text = ast.get_source_segment(source, node)
    if text is None:
        return False
    opening = len(text) - len(text.lstrip("rRuU"))
    quote = 3 if text[opening : opening + 3] in ('"""', "'''") else 1
    return text[opening + quote : len(text) - quote] == node.value


def _dump_without_docstrings(source: str) -> tuple[str, list[Docstring]]:
    """The AST dump with every docstring blanked, and the docstrings in walk order, so equal dumps pair them up."""
    tree = ast.parse(source)
    lines = source.split("\n")  # decode_source leaves only newlines, and the AST numbers lines by them
    found: list[Docstring] = []
    for node in ast.walk(tree):
        if not isinstance(node, CFG.DOCSTRING_OWNERS) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            prefix = lines[first.lineno - 1].encode()[: first.col_offset].decode()
            indent = None if prefix.strip(" \t") else prefix
            found.append(Docstring(first.value.value, indent, _is_plain_literal(source, first.value)))
            first.value.value = ""
    return ast.dump(tree), found


def _docstrings_match(before: list[Docstring], after: list[Docstring]) -> bool:
    """Each head docstring is its base's text, or exactly ruff's re-layout of a plain base at the head's indent. One
    way, as ruff only strips and re-indents: a head that adds whitespace ruff would strip is a real edit."""
    return len(before) == len(after) and all(
        new.text == old.text
        or (old.plain and new.indent is not None and new.text == _ruff_docstring(old.text, new.indent))
        for old, new in zip(before, after, strict=True)
    )


def _node_bounds(tree: ast.Module) -> tuple[list[int], list[int]]:
    """The first line of every positioned node and the last line of every node that ends on its own token, each
    sorted. Equal ASTs have equal node sets, and a comment runs to the end of its line, so a node on a comment's
    row opens and closes before it."""
    nodes = list(ast.walk(tree))
    starts = sorted(node.lineno for node in nodes if isinstance(node, CFG.POSITIONED_NODES))
    ends = sorted(node.end_lineno or node.lineno for node in nodes if isinstance(node, CFG.OWN_END_NODES))
    return starts, ends


def _ruff_comment(text: str) -> str:
    """What ruff format makes of a comment: trailing whitespace goes (ruff's set, so a trailing U+001C stays), a
    leading no-break space becomes a space, and `#x` gains one space unless x is ! : # or ' (shebangs, Sphinx,
    banners). Inner spacing is kept, because `# no sec` and `# nosec` differ to the tools that read them."""
    body = text.rstrip(CFG.RUFF_WHITESPACE)[1:]
    if body.startswith("\u00a0"):
        body = " " + body[1:]
    if body and not body.startswith((" ", "!", ":", "#", "'")):
        body = " " + body
    return "#" + body


def _is_fixed_token(tok: tokenize.TokenInfo) -> bool:
    """A token ruff never adds or drops: a name, keyword, number or operator, but not a string, which it joins."""
    if tok.type == tokenize.OP:
        return tok.string not in CFG.MOVABLE_OPERATORS
    return tok.type in (tokenize.NAME, tokenize.NUMBER)


def _string_pieces_before_each_comment(tokens: list[tokenize.TokenInfo]) -> list[int]:
    """Per comment, in order: how many pieces of an implicitly concatenated string precede it when it sits between
    two of them, else 0. The pieces make one node, so its bounds cannot tell `"a"  # c` then `"b"` from `"a"` then
    `"b"  # c`, and ruff joins pieces only when no comment sits between them, so this index is fixed."""
    positions: list[int] = []
    waiting: list[int] = []  # comments after a piece, until the next token shows whether another piece follows
    run = depth = 0
    for tok in tokens:
        if tok.type == tokenize.COMMENT and depth:
            positions.append(0)  # inside an f-string's replacement field, which 3.12 allows: not between pieces
        elif tok.type == tokenize.COMMENT:
            waiting.append(len(positions))
            positions.append(run)
        elif depth:  # a field's names and operators are not pieces; _comments counts them as fixed tokens
            depth += (tok.type in CFG.PIECE_OPENERS) - (tok.type in CFG.PIECE_CLOSERS)
        elif tok.type in (tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
            continue
        elif tok.type == tokenize.STRING or tok.type in CFG.PIECE_OPENERS:
            depth = int(tok.type in CFG.PIECE_OPENERS)
            run += 1
            waiting.clear()
        else:
            for index in waiting:
                positions[index] = 0
            waiting.clear()
            run = 0
    return positions


def _comments_match(before: list[tuple[Place, str]], after: list[tuple[Place, str]]) -> bool:
    """Same places, and each head comment is its base comment untouched or exactly what ruff makes of it. One-way,
    because ruff only strips and adds space: `# x` to `#x`, or a trailing space added, is no ruff output, while a
    comment inside `# fmt: off`, which ruff leaves alone, still matches."""
    return len(before) == len(after) and all(
        place_before == place_after and text_after in (text_before, _ruff_comment(text_before))
        for (place_before, text_before), (place_after, text_after) in zip(before, after, strict=True)
    )


def _comments(source: str) -> list[tuple[Place, str]]:
    """Every comment in order, as (place, text): its line is layout, its words and place are not.

    Its place in the tree is how many AST nodes open and close before it, which counts statements, strings and `...`
    alike and survives ruff adding or dropping parentheses, commas and string pieces, and how many fixed tokens
    precede it: names, keywords, numbers and operators, which ruff never adds or drops, and which cover `else:` and
    `+`, since neither has a node. ruff does move a trailing operator past an end-of-line comment (`a +  # c` then
    `b` becomes `a  # c` then `+ b`), and that fails here, the safe side. ruff keeps an end-of-line comment at the end
    of a line, and coverage reads `if x:  # pragma: no cover` as the whole block but a comment-only line as nothing,
    so that is part of it too. One node can span several string pieces, so the piece a comment follows counts too.
    What is left uncounted is `( ) , ;`, which ruff adds and drops, and a comment moved across only those stays on
    the same line of the same code."""
    starts, ends = _node_bounds(ast.parse(source))
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    pieces = iter(_string_pieces_before_each_comment(tokens))
    found: list[tuple[Place, str]] = []
    fixed_tokens = 0
    prev_row = 0  # A comment is own-line when the token before it ended on an earlier row: only code can end on its.
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            row = tok.start[0]
            place = (
                bisect.bisect_right(starts, row),
                bisect.bisect_right(ends, row),
                fixed_tokens,
                prev_row != row,
                next(pieces),
            )
            found.append((place, tok.string))
        elif _is_fixed_token(tok):
            fixed_tokens += 1
        prev_row = tok.end[0]
    return found


def _runs_a_shebang(data: bytes) -> bool:
    """The kernel reads a shebang only at byte 0, so its POSITION is semantics. Its words are a comment's."""
    return data.startswith(b"#!")


# ----- commands -------------------------------------------------------------


def prove(repo: Path, base: str, head: str) -> Result:
    entries = _changed_entries(repo, base, head)
    not_modified = [f"{status} {path}" for status, path, _, _ in entries if status != "M"]
    if not_modified:
        return Result(1, lines=[f"[format-proof] FAIL not format-only, files added/deleted: {x}" for x in not_modified])
    mode_changed = [f"{path} {old} -> {new}" for _, path, old, new in entries if old != new]
    if mode_changed:
        return Result(1, lines=[f"[format-proof] FAIL not format-only, file mode changed: {x}" for x in mode_changed])
    py_paths = [path for _, path, _, mode in entries if path.endswith(".py") and mode in CFG.REGULAR_FILE_MODES]
    provable = set(py_paths)
    unprovable = [
        f"[format-proof] UNKNOWN {path} (mode {mode}): not a regular .py file, so no AST can prove it"
        for _, path, _, mode in entries
        if path not in provable
    ]
    if not py_paths:
        return Result(2, lines=["[format-proof] UNKNOWN no regular .py file changed; nothing was proven", *unprovable])
    result = Result(0, files_checked=len(py_paths))
    for path in py_paths:
        data_before, data_after = _blob(repo, base, path), _blob(repo, head, path)
        if _runs_a_shebang(data_before) != _runs_a_shebang(data_after):
            result.exit_code = 1
            result.lines.append(f"[format-proof] FAIL {path}: the shebang moved on or off byte 0, where it runs")
            continue
        try:
            before, after = importlib.util.decode_source(data_before), importlib.util.decode_source(data_after)
            (dump_before, docs_before), (dump_after, docs_after) = (
                _dump_without_docstrings(before),
                _dump_without_docstrings(after),
            )
            comments_equal = _comments_match(_comments(before), _comments(after))
        except (SyntaxError, UnicodeDecodeError) as exc:
            return Result(2, lines=[f"[format-proof] UNKNOWN {path} does not decode or parse: {exc}"])
        if dump_before != dump_after or not _docstrings_match(docs_before, docs_after):
            result.exit_code = 1
            result.lines.append(f"[format-proof] FAIL {path}: AST differs, this is not a format-only change")
        elif not comments_equal:
            result.exit_code = 1
            result.lines.append(f"[format-proof] FAIL {path}: a comment was added, removed, reworded or moved")
        elif [doc.text for doc in docs_before] != [doc.text for doc in docs_after]:
            result.docstring_normalized += 1
    if result.exit_code == 0 and unprovable:
        result.exit_code = 2
        result.lines.extend(unprovable)
    if result.exit_code == 0:
        result.lines.append(
            f"[format-proof] OK {result.files_checked} .py files AST-equal "
            f"({result.docstring_normalized} with a docstring ruff re-laid), "
            f"base={_git(repo, 'rev-parse', base).stdout.strip()} head={_git(repo, 'rev-parse', head).stdout.strip()}"
        )
    return result


def _identity_verdict(repo: Path, sha: str, shallow: bool) -> tuple[int, list[str]]:
    """(0, []) when a listed SHA is a commit in HEAD whose subject claims format-only, else (1 or 2, why)."""
    kind = _git(repo, "cat-file", "-t", sha, check=False)
    if kind.returncode != 0 and shallow:
        return 2, [f"[ignore-revs] UNKNOWN {sha} not visible in this shallow clone"]
    if kind.returncode != 0:
        return 1, [f"[ignore-revs] FAIL {sha} is not a commit in this repository"]
    if kind.stdout.strip() != "commit":
        # Not `<sha>^{commit}`: that peels a tag, and blame skips only the commit's own SHA.
        return 1, [f"[ignore-revs] FAIL {sha} is a {kind.stdout.strip()} object, not a commit"]
    if _git(repo, "merge-base", "--is-ancestor", sha, "HEAD", check=False).returncode != 0:
        return 1, [f"[ignore-revs] FAIL {sha} is not an ancestor of HEAD (squash or rebase merge?)"]
    subject = _git(repo, "log", "-1", "--format=%s", sha).stdout.strip()
    if not subject.startswith(CFG.FORMAT_SUBJECT_PREFIX):
        return 1, [f"[ignore-revs] FAIL {sha} subject is not {CFG.FORMAT_SUBJECT_PREFIX}: {subject!r}"]
    return 0, []


def _proof_verdict(repo: Path, sha: str, shallow: bool) -> tuple[int, list[str]]:
    """(0, []) when a listed commit has one parent and `prove` passes against it: the subject is only a claim."""
    parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()[1:]
    if not parents and shallow:
        return 2, [f"[ignore-revs] UNKNOWN {sha}: this shallow clone cuts off its parent"]
    if len(parents) != 1:
        return 1, [f"[ignore-revs] FAIL {sha} has {len(parents)} parents, so no single diff proves it"]
    proof = prove(repo, parents[0], sha)
    if proof.exit_code == 1:
        return 1, [f"[ignore-revs] FAIL {sha} is not format-only against its parent:", *proof.lines]
    if proof.exit_code == 2:
        return 2, [f"[ignore-revs] UNKNOWN {sha} cannot be proven format-only against its parent:", *proof.lines]
    return 0, []


def check_ignore_revs(repo: Path, path: Path) -> Result:
    shas: list[str] = []
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not CFG.SHA_RE.match(line):
            return Result(1, lines=[f"[ignore-revs] FAIL line {number}: not a full 40-hex SHA: {line!r}"])
        shas.append(line)
    shallow = _git(repo, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    result = Result(0, files_checked=len(shas))
    unknown: list[str] = []
    for sha in shas:
        verdict, lines = _identity_verdict(repo, sha, shallow)
        if verdict == 0:
            verdict, lines = _proof_verdict(repo, sha, shallow)
        if verdict == 1:
            result.exit_code = 1
            result.lines.extend(lines)
        elif verdict == 2:
            unknown.extend(lines)
    if result.exit_code == 0 and unknown:
        result.exit_code = 2
        result.lines.extend(unknown)
    if result.exit_code == 0:
        result.lines.append(f"[ignore-revs] OK {len(shas)} listed commit(s), each in HEAD and proven format-only")
    return result


# ----- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_prove = sub.add_parser("prove", help="prove base..head changed Python layout only")
    p_prove.add_argument("--base", required=True)
    p_prove.add_argument("--head", default="HEAD")
    p_revs = sub.add_parser("ignore-revs", help="check every SHA in the blame ignore file")
    p_revs.add_argument("--file", type=Path, default=REPO / CFG.IGNORE_REVS_FILE)
    args = parser.parse_args(argv)
    if args.command == "prove":
        result = prove(REPO, args.base, args.head)
    elif args.command == "ignore-revs":
        result = check_ignore_revs(REPO, args.file)
    else:
        raise SystemExit(f"unhandled command {args.command}")
    print("\n".join(result.lines))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
