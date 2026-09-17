"""Find every `node --test` invocation in this repository.

The scanner behind `tests/scripts/test_node_test_tap_reporter.py`. It lives in a
module of its own because it is real code with real edge cases (quoting, shell
separators, line continuations, composed Python argv, folded YAML), and a test
file that had grown to hold all of it crossed this repository's own file size
limit. Tests import from here; the rules it enforces are documented on the
functions themselves.

Two invariants run through the whole file and are worth stating once:

* Detection fails OPEN and certification fails CLOSED. A shape this scanner
  cannot read must never be reported as a file holding no call, and must never
  be cleared as compliant either.
* A surface that cannot be parsed is an error, not an empty result.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Executable surfaces only. specs/ and .planning/ are write-once prose records
# and are deliberately out of scope.
# Scope is DISCOVERED, not listed. Naming five locations meant a `node --test`
# added in another app's package file, another root script, or .github/actions
# was invisible: the census stayed pinned, the offender test stayed green, and
# nothing failed. A rule cannot go stale the way a list does, so a surface is in
# scope when its NAME says it carries commands, wherever it lives, or when it
# sits under a directory whose whole purpose is executable material.
SURFACE_NAMES = frozenset({"Makefile", "justfile", "package.json"})
SURFACE_DIRS = ("scripts", ".github")
# Pruned while walking: vendored or generated trees are not this repository's
# call sites, and node_modules alone would dominate the walk.
EXCLUDED_DIRS = frozenset({
    ".git", ".svelte-kit", ".venv", "build", "data", "dist", "node_modules", "target",
})

# Scope is a DENY list, not an allow list. An allow list of executable suffixes
# silently omits whatever nobody thought of -- .js, .cjs, .bash today, something
# else tomorrow -- and a new `node --test` in one of those files would be
# invisible, leave the census unchanged, and never fail this guard. Defaulting to
# SCAN means a new format is covered the day it appears; a new BINARY format is a
# hard failure naming the file, which is the fail-closed direction.
BINARY_SUFFIXES = frozenset({".pyc", ".m4a"})
# Prose is text and would tokenize cleanly, but a documented command is not a
# call site and counting one would put a false entry in the census.
PROSE_SUFFIXES = frozenset({".md", ".html", ".txt", ".example"})
UNSCANNED_SUFFIXES = BINARY_SUFFIXES | PROSE_SUFFIXES

TEST_FLAG = "--test"
TAP_FLAG = "--test-reporter=tap"
# Where one command ends and the next begins. `node --test x; echo
# --test-reporter=tap` must not read as compliant.
SEPARATORS = frozenset({";", "&&", "||", "|", "&"})
# shlex leaves `;` glued to the token before it, so `node --test x; echo ...`
# would read as one command. Declaring these as punctuation makes them their own
# tokens. The set is deliberately NOT shlex's default `();<>|&`: the Makefile
# calls `$(FRONTEND_NODE)`, and treating parentheses as punctuation would shred
# that token and lose the site.
_PUNCTUATION = ";|&"
#: Marks argv that whitespace splitting produced after shlex refused the
#: line. Such argv may name a call site but may never certify one. The NUL
#: keeps it impossible to spell in a real command.
_UNPARSED = "\x00unparsed"

# A token that invokes node: the bare binary, a path ending in it, or a variable
# expanding to it. The Makefile calls `$(FRONTEND_NODE)`, so a word-boundary
# match on `node` would skip that site entirely.
def _is_node_token(token: str) -> bool:
    """Does this argv token invoke node?

    Covers the bare binary, a path-qualified one, the Windows `node.exe`
    spelling (.ps1 and .cmd are declared in scope, so it can appear), and a
    variable expanding to it -- the Makefile calls `$(FRONTEND_NODE)`, where a
    word boundary before `node` would skip the site entirely.
    """
    tok = token.strip().strip("\"'")
    tok = tok.rstrip(")}")                      # $(FRONTEND_NODE) -> $(FRONTEND_NODE
    tok = re.split(r"[\\/]", tok)[-1]            # path or Windows path -> basename
    if tok.lower().endswith(".exe"):
        tok = tok[: -len(".exe")]
    if tok.lower() == "node":
        return True
    # A VARIABLE expanding to node, e.g. `$(FRONTEND_NODE)` or `%NODE%`. Gated
    # on variable syntax: a bare word merely ending in those four letters, such
    # as `notnode`, is a different program, and accepting it would put a false
    # entry in the census.
    looks_like_variable = "$" in tok or "%" in tok or "{" in tok
    return looks_like_variable and tok.upper().strip("%${}(") .endswith("NODE")
# Quoted string literals, used to bound commands inside JSON and YAML. Each value
# is its own command, which is what stops one line's flags reaching another's.
#: A folded workflow command (`run: >`) joins its lines into one command with
#: no continuation marker, so a line-oriented scan sees `node` and `--test` as
#: separate lines and finds no call at all. The repository uses the literal form
#: (`run: |`) everywhere, so rather than grow a YAML parser this form is REFUSED:
#: an unsupported command shape must fail rather than read as absent.
_FOLDED_RUN = re.compile(r"^\s*-?\s*run:\s*>")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _in_scope(path: Path) -> bool:
    return path.suffix not in UNSCANNED_SUFFIXES


def _candidate_files() -> list[Path]:
    """Every command-carrying surface in the repository, discovered by rule."""
    files: list[Path] = []
    for root, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        here = Path(root)
        rel = here.relative_to(REPO_ROOT)
        in_surface_dir = rel.parts[:1] and rel.parts[0] in SURFACE_DIRS
        for name in filenames:
            path = here / name
            if not _in_scope(path):
                continue
            if name in SURFACE_NAMES or in_surface_dir:
                files.append(path)
    return sorted(files)


def _display(path: Path) -> str:
    """Never let the reporting path raise: a fixture outside the repo has no
    relative form, and a ValueError there would replace the message with an
    unrelated traceback."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _read_or_fail(path: Path) -> str:
    """No except-and-continue: an unreadable surface is UNMEASURED, not clean."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        raise AssertionError(
            f"cannot scan {_display(path)} ({exc.__class__.__name__}: {exc}). "
            "An executable surface that cannot be read is unmeasured, not clean. If this "
            "is a new binary format, add its suffix to BINARY_SUFFIXES deliberately; do "
            "not restore a silent skip."
        ) from exc


def _tokenize(command: str) -> list[str]:
    """argv for one command string, falling back to whitespace splitting.

    shlex raises on an unbalanced quote, which a Makefile recipe or a templated
    workflow line can legitimately contain (`echo don't`). Dropping such a line
    would be a silent skip, so it stays MEASURED.

    But a fallback tokenization must never CERTIFY. Whitespace splitting does not
    know quoting, so `node --test "unbalanced; echo --test-reporter=tap` yields a
    bare `--test-reporter=tap` token and the call reads as compliant while node
    still runs the default reporter, which is the one direction a guard must
    never fail in. The fallback therefore carries `_UNPARSED`, and compliance
    requires its absence: a call found this way can be reported, never cleared.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return [*command.split(), _UNPARSED]


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Ids of every docstring constant, which is prose rather than a call site.

    savepoint_gate.py says `node --test` in a sentence, and a sentence must not
    read as an invocation.
    """
    ids: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))
    return ids


def _argv_parts(node: ast.AST, consumed: set[int]) -> list[str] | None:
    """The constant strings of a possibly concatenated argv expression, in order.

    Returns None when the expression is not argv-shaped. Every list or tuple it
    absorbs is recorded in `consumed` so the caller does not emit it a second
    time on its own.
    """
    if isinstance(node, (ast.List, ast.Tuple)):
        consumed.add(id(node))
        return [
            e.value for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _argv_parts(node.left, consumed)
        right = _argv_parts(node.right, consumed)
        if left is None and right is None:
            return None
        return (left or []) + (right or [])
    return None


def _python_commands(path: Path, text: str) -> list[tuple[int, list[str]]]:
    """argv lists spelled as Python list/tuple literals of string constants.

    savepoint_gate.py builds its argv this way, and the literal may be wrapped
    across lines, which is why a line-oriented scan missed it. Docstrings are
    skipped: that same file says `node --test` in a sentence, and prose must not
    read as a call site.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise AssertionError(
            f"cannot parse {_display(path)} as Python ({exc.__class__.__name__}: {exc}). "
            "An unparseable surface must not read as one with no `node --test` call: "
            "that is a silent skip of an executable surface, not a clean result."
        ) from exc
    docstrings = _docstring_ids(tree)
    found: list[tuple[int, list[str]]] = []
    # An argv expression is read as a WHOLE, because three separate findings here
    # were three instances of one class: a shape this scanner could not fully read
    # reported as a file with no call. `["node"] + ["--test", *files]` is the
    # composed case, where neither literal alone holds both tokens. Concatenation
    # is flattened, and the inner literals are then not re-emitted on their own,
    # which would double count the census.
    consumed: set[int] = set()
    composed: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            parts = _argv_parts(node, consumed)
            if parts is not None and parts:
                composed.append((node.lineno, parts))
    found.extend(composed)
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            if id(node) in consumed:
                continue
            # The CONSTANT elements, in order, rather than only all-constant
            # lists: `["node", "--test", *files]` is an ordinary invocation, and
            # requiring every element to be a literal dropped it entirely, so a
            # non-TAP call in that form stayed green. Reading the literals keeps
            # both directions right, because a reporter spelled after the dynamic
            # part is still seen. A flag that is itself dynamic cannot be read by
            # any static scan, and is reported as missing rather than assumed.
            parts = [
                e.value
                for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            if parts:
                found.append((node.lineno, parts))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and "node" in node.value.lower()
        ):
            found.append((node.lineno, _tokenize(node.value)))
    return found


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Physical lines joined on a trailing backslash, keyed by their FIRST line.

    A shell, Make or YAML command may wrap: `node \\` then `--test tests/unit`
    on the next line. Scanning physical lines drops that invocation from the
    census entirely while every guard stays green, which is the silent-skip
    class this file exists to catch.
    """
    out: list[tuple[int, str]] = []
    pending: list[str] = []
    start = 1
    for lineno, line in enumerate(text.split("\n"), start=1):
        if not pending:
            start = lineno
        if line.rstrip().endswith("\\"):
            pending.append(line.rstrip()[:-1])
            continue
        pending.append(line)
        out.append((start, " ".join(pending)))
        pending = []
    if pending:
        out.append((start, " ".join(pending)))
    return out


def _commands(path: Path, text: str) -> list[tuple[int, list[str]]]:
    """Every command in one file, as (line number, argv)."""
    if path.suffix == ".py":
        return _python_commands(path, text)
    commands: list[tuple[int, list[str]]] = []
    if path.suffix in {".yml", ".yaml"}:
        folded = [
            lineno
            for lineno, line in enumerate(text.split("\n"), start=1)
            if _FOLDED_RUN.match(line)
        ]
        if folded:
            raise AssertionError(
                f"{_display(path)} uses a folded command scalar at line(s) "
                f"{', '.join(str(n) for n in folded)}. This scanner reads lines, so a "
                "folded command would be split across them and found nowhere. Spell it "
                "as a literal block (`run: |`), which every other command here uses, or "
                "teach the scanner the folded form. An unsupported shape must not read "
                "as a file with no `node --test` call."
            )
    if path.suffix == ".json":
        # A JSON line is `"key": "command",`, so the surrounding syntax would
        # read as tokens: here the quoted string VALUES are the commands. This
        # extraction is confined to JSON deliberately. Applying it wherever a
        # line happened to contain a quote discarded the rest of that line, so
        # `node --test "$FILE"` scanned as the single token `$FILE` and the
        # invocation vanished from both the census and the offender check.
        try:
            json.loads(text)
        except ValueError as exc:
            raise AssertionError(
                f"cannot parse {_display(path)} as JSON ({exc}). An unparseable "
                "surface must not read as one with no `node --test` call."
            ) from exc
        for lineno, line in _logical_lines(text):
            commands.extend((lineno, _tokenize(value)) for value in _QUOTED.findall(line))
        return commands
    for lineno, line in _logical_lines(text):
        if line.lstrip().startswith("#"):
            # A whole-line comment is documentation. Only whole-line: an inline
            # `#` can sit inside a quoted argument, and truncating there would
            # hide a real invocation, which is the false-negative direction.
            continue
        commands.append((lineno, _tokenize(line)))
    return commands


def _segments(argv: list[str]) -> list[list[str]]:
    """Cut argv at shell separators so one command's flags stay its own."""
    out: list[list[str]] = [[]]
    for token in argv:
        if token in SEPARATORS:
            out.append([])
        else:
            out[-1].append(token)
    return [seg for seg in out if seg]


def _node_test_sites_in(label: str, path: Path, text: str) -> list[tuple[str, int, list[str]]]:
    return [
        (label, lineno, segment)
        for lineno, argv in _commands(path, text)
        for segment in _segments(argv)
        if TEST_FLAG in segment and any(_is_node_token(tok) for tok in segment)
    ]


def _scan(label: str, text: str, suffix: str = ".sh") -> list[tuple[str, int, list[str]]]:
    """Run the scan over a crafted command. A guard whose only fixture is the
    repo can confirm the status quo and nothing else."""
    return _node_test_sites_in(label, Path(f"fixture{suffix}"), text)


def _node_test_sites() -> list[tuple[str, int, list[str]]]:
    sites: list[tuple[str, int, list[str]]] = []
    for path in _candidate_files():
        sites.extend(_node_test_sites_in(_display(path), path, _read_or_fail(path)))
    return sites


# The census as measured on this tree. A floor ("at least five") is too weak to
# be a control: it stays green when the scan LOSES a site and gains a spurious
# one, which is exactly what happened during an earlier round. Per-file counts
# catch both directions.
