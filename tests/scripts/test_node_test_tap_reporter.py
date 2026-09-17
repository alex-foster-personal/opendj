"""Every `node --test` invocation must use the TAP reporter.

Node's default `spec` reporter joins every failed and cancelled result into one
string to print the summary. A large cancellation cascade overflows V8's
maximum string length and the reporter dies with `RangeError: Invalid string
length` WHILE PRINTING THE FAILURE, so nothing is printed at all and the real
failure is invisible. A missed invocation is therefore worse than an ordinary
missed fix: the gate still goes red, but it goes red with no test identities
(see issue #3466, and the Codex BLOCKING finding on PR #3468).

This guards the CLASS rather than the sites known when it was written -- a
seventh invocation added later is caught here instead of at the next cascade.

The check works on ARGV, not on raw text. Four review rounds of regex heuristics
(a window, a gap rule, a keyword filter) each fixed one case and left a
neighboring one open, because a command line is not a string that happens to
contain flags. Extracting the command, splitting it into tokens, and cutting it
at shell separators makes an entire class of those defects unrepresentable:
`--test` cannot match the prefix of `--test-reporter=tap` when it is compared as
a whole token, and a reporter flag in a LATER command cannot be credited to this
one when the segment ends at the separator between them.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Executable surfaces only. specs/ and .planning/ are write-once prose records
# and are deliberately out of scope.
SEARCH_PATHS = (
    "Makefile",
    "justfile",
    "apps/webui/frontend/package.json",
    "scripts",
    ".github/workflows",
)

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
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _in_scope(path: Path) -> bool:
    return path.suffix not in UNSCANNED_SUFFIXES


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for entry in SEARCH_PATHS:
        target = REPO_ROOT / entry
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(p for p in target.rglob("*") if p.is_file() and _in_scope(p))
    return files


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
    workflow line can legitimately contain. Falling back to `.split()` keeps such
    a line MEASURED; dropping it would be a silent skip.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return command.split()


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
    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    found: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            parts = [
                e.value
                for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            if parts and len(parts) == len(node.elts):
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
EXPECTED_SITES_PER_FILE = {
    "Makefile": 1,
    "apps/webui/frontend/package.json": 4,
    "scripts/savepoint_gate.py": 1,
}


def test_the_scan_finds_the_known_invocations() -> None:
    """A zero is both a value and an error signature, so pin the census."""
    counts: dict[str, int] = {}
    for label, _lineno, _argv in _node_test_sites():
        counts[label] = counts.get(label, 0) + 1
    assert counts == EXPECTED_SITES_PER_FILE, (
        "the `node --test` census moved. Finding FEWER means the scan broke, not "
        "that the tree is clean; finding MORE may be a real new call site or a "
        "non-runner `node` miscounted. Identify which before editing this number:\n"
        f"  expected {EXPECTED_SITES_PER_FILE}\n  measured {counts}"
    )


def test_every_node_test_invocation_uses_the_tap_reporter() -> None:
    offenders = [s for s in _node_test_sites() if TAP_FLAG not in s[2]]
    assert not offenders, (
        "these `node --test` call sites still use the default spec reporter, which "
        "dies with RangeError while printing a large cancellation cascade and hides "
        "the failure entirely:\n"
        + "\n".join(f"  {label}:{lineno}: {' '.join(argv)}" for label, lineno, argv in offenders)
    )


def test_the_reporter_must_belong_to_the_same_command() -> None:
    """Compliance was previously satisfied by the flag appearing anywhere nearby,
    so a reporter in a LATER command certified this one."""
    assert _scan("f", "node --test tests/unit") != [], "sanity: this is a runner call"
    trailing = _scan("f", "node --test tests/unit; echo --test-reporter=tap")
    assert trailing, "the invocation must still be found"
    assert all(TAP_FLAG not in argv for _, _, argv in trailing), (
        "the reporter flag belongs to the `echo` after the separator; crediting it "
        "here certifies a command that still runs the default reporter"
    )
    same = _scan("f", "node --test --test-reporter=tap x")
    assert any(TAP_FLAG in argv for _, _, argv in same), (
        "a reporter in the SAME command must still count, or the rule rejects everything"
    )


def test_options_taking_a_separate_value_do_not_hide_the_call() -> None:
    """`node --import tsx --test` is an ordinary invocation. A rule that allowed
    only flag-shaped tokens before `--test` dropped it from the census entirely,
    so adding a non-TAP call in that form would have stayed green."""
    for command in (
        "node --import tsx --test",
        "node --require setup.js --test tests/unit",
        "node --experimental-vm-modules --test",
    ):
        assert _scan("f", command), f"{command!r} is a runner call and must be found"


def test_every_standalone_runner_form_is_recognized() -> None:
    """An earlier filter required `.test.mjs` or `--test-concurrency` nearby, so
    these evaded the census AND the reporter assertion."""
    for command in (
        "node --test tests/unit",
        "node --test foo.test.js",
        "node --test",
    ):
        found = _scan("f", command)
        assert len(found) == 1, f"{command!r} was not recognized as a runner call"
        assert TAP_FLAG not in found[0][2], "fixture sanity: this form has no reporter"


def test_a_reporter_only_command_is_not_a_runner_call() -> None:
    """`--test` is a PREFIX of `--test-reporter` and `--test-concurrency`. Token
    equality makes that confusion unrepresentable; this pins it."""
    for command in (
        "node --test-reporter=tap tests/unit/x.test.mjs",
        "node --test-concurrency=4 tests/unit/x.test.mjs",
    ):
        assert _scan("f", command) == [], (
            f"{command!r} has no standalone --test flag, so node does not run the "
            "test runner; counting it certifies a command that cannot fail this guard"
        )


def test_a_flag_belonging_to_a_later_command_is_not_this_ones() -> None:
    """package.json:25 is `node scripts/build-design-system.mjs`; the next line's
    flags must not reach it."""
    # A whole document, not a fragment: the scanner fails closed on JSON it
    # cannot parse, and a fixture that could not stand in for the real surface
    # would be testing a shape package.json never has.
    package_json = (
        "{\n"
        '  "scripts": {\n'
        '    "design-system:build": "node scripts/build-design-system.mjs",\n'
        '    "test:design-system": "node --test --test-reporter=tap x.test.mjs"\n'
        "  }\n"
        "}\n"
    )
    found = _scan("f", package_json, suffix=".json")
    assert [lineno for _, lineno, _ in found] == [4], (
        f"only the second script is a runner call, got lines {[n for _, n, _ in found]}"
    )
    assert _scan("f", "node build.mjs --watch && tsc --test") == [], (
        "the --test here is an argument of tsc, in a separate command"
    )


def test_a_continued_command_is_still_one_invocation() -> None:
    """A command wrapped on a trailing backslash was omitted from the census
    entirely, so a default-reporter call could be added with both guards green
    (Sol P1, fifth round)."""
    wrapped = "node \\\n  --test tests/unit\n"
    found = _scan("f", wrapped)
    assert len(found) == 1, f"the continued invocation was not found: {found}"
    assert TAP_FLAG not in found[0][2], "fixture sanity: this form has no reporter"
    assert found[0][1] == 1, "the site should be reported at the line the command starts on"
    # Control: a backslash that is NOT a continuation must not glue two
    # unrelated commands into one.
    separate = "echo done\nnode --test --test-reporter=tap x.test.mjs\n"
    assert [n for _, n, _ in _scan("f", separate)] == [2]


def test_the_windows_node_exe_spelling_is_recognized() -> None:
    """.ps1 and .cmd are declared in scope, so `node.exe --test` can appear
    there and must not be invisible (Sol P2, fifth round)."""
    for command in (
        "node.exe --test tests/unit",
        r"C:\\tools\\node.exe --test tests/unit",
        "./node --test tests/unit",
    ):
        assert _scan("f", command), f"{command!r} invokes node and must be found"
    assert _scan("f", "notnode --test x") == [], "a token merely ending in `node` text is not node"


def test_a_documented_command_is_not_a_call_site(tmp_path: Path) -> None:
    """savepoint_gate.py's module docstring says `node --test` in a sentence.
    Prose must not read as a non-compliant call, while a string in a list
    literal is code and must survive."""
    module = tmp_path / "m.py"
    module.write_text(
        '"""Without the flag node --test would hang forever."""\n'
        "# another mention: node --test tests/unit\n"
        'ARGV = ["node", "--test", "--test-reporter=tap"]\n'
    )
    found = _node_test_sites_in("m.py", module, module.read_text())
    assert [lineno for _, lineno, _ in found] == [3], (
        f"expected only the argv literal on line 3, got {found}"
    )


def test_an_unreadable_surface_fails_instead_of_being_skipped(tmp_path: Path) -> None:
    """An earlier version caught UnicodeDecodeError/OSError and continued, so an
    unscannable file read exactly like a compliant one."""
    binary = tmp_path / "unreadable.py"
    binary.write_bytes(b"node --test \xff\xfe not utf-8")
    with pytest.raises(AssertionError, match="cannot scan"):
        _read_or_fail(binary)


def test_a_quoted_argument_does_not_hide_the_call() -> None:
    """A quoted argument is an argument, not the whole command.

    The scanner used to take a line's quoted substrings INSTEAD of the line
    whenever it contained any, so `node --test "$FILE"` scanned as the lone
    token `$FILE`: the invocation disappeared from the census AND from the
    offender check, which is the false-negative direction this guard exists to
    prevent.
    """
    for command in (
        'node --test "$FILE"',
        'node --test "tests/unit" --test-reporter=tap',
        '"$(FRONTEND_NODE)" --test "tests/unit"',
    ):
        assert _scan("f", command), f"{command!r} is a runner call and must be found"

    bare = _scan("f", 'node --test "$FILE"')
    assert all(TAP_FLAG not in argv for _, _, argv in bare), (
        "this call has no reporter and must be reportable as an offender"
    )
    compliant = _scan("f", 'node --test "tests/unit" --test-reporter=tap')
    assert any(TAP_FLAG in argv for _, _, argv in compliant), (
        "a quoted argument must not cost a compliant call its reporter either"
    )


def test_an_unparseable_python_surface_fails_instead_of_being_skipped(tmp_path: Path) -> None:
    """A SyntaxError used to return no commands, so a broken executable surface
    read exactly like one containing no runner call."""
    broken = tmp_path / "broken.py"
    broken.write_text('subprocess.run(["node", "--test"]\n', encoding="utf-8")
    with pytest.raises(AssertionError, match="cannot parse"):
        _commands(broken, broken.read_text(encoding="utf-8"))


def test_an_unparseable_json_surface_fails_instead_of_being_skipped(tmp_path: Path) -> None:
    """Same class on the JSON surface: package.json is where four of the six
    known call sites live, so it may never read as empty by accident."""
    broken = tmp_path / "package.json"
    broken.write_text('{"scripts": {"test:unit": "node --test"\n', encoding="utf-8")
    with pytest.raises(AssertionError, match="cannot parse"):
        _commands(broken, broken.read_text(encoding="utf-8"))


def test_declared_scope_excludes_the_binaries_that_actually_live_here() -> None:
    """Fail-closed must not reject legitimate absence: the repo really does carry
    .pyc and .m4a under these roots, so they are out of scope BY NAME. Everything
    not named is scanned."""
    for suffix in (".pyc", ".m4a"):
        assert suffix in BINARY_SUFFIXES, f"{suffix} is binary and cannot be scanned as text"
    scanned = _candidate_files()
    assert scanned, "scope resolved to zero files"
    assert not [p for p in scanned if p.suffix in UNSCANNED_SUFFIXES]
    for suffix in (".js", ".cjs", ".bash", ".ps1", ".cmd"):
        assert _in_scope(Path(f"scripts/whatever{suffix}")), (
            f"{suffix} is not scanned, so a `node --test` in one would be invisible"
        )
