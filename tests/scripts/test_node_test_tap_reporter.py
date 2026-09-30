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

import subprocess
from pathlib import Path

import pytest

from scripts.node_test_reporter_scan import (
    _UNPARSED,
    BINARY_SUFFIXES,
    EXCLUDED_DIRS,
    REPO_ROOT,
    TAP_FLAG,
    UNSCANNED_SUFFIXES,
    _candidate_files,
    _commands,
    _in_scope,
    _node_test_sites,
    _node_test_sites_in,
    _read_or_fail,
    _scan,
    _tokenize,
)

EXPECTED_SITES_PER_FILE = {
    "Makefile": 1,
    "apps/desktop/electron/package.json": 2,
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


def _offenders(
    sites: list[tuple[str, int, list[str]]],
) -> list[tuple[str, int, list[str]]]:
    """Call sites this scanner cannot clear.

    Named once and shared with the fixture tests deliberately: when the fixture
    test spelled this predicate out again, mutating the real one left the fixture
    test green, so the check was carrying an assumption nothing verified.
    """
    return [s for s in sites if TAP_FLAG not in s[2] or _UNPARSED in s[2]]


def test_every_node_test_invocation_uses_the_tap_reporter() -> None:
    offenders = _offenders(_node_test_sites())
    assert not offenders, (
        "these `node --test` call sites still use the default spec reporter, which "
        "dies with RangeError while printing a large cancellation cascade and hides "
        "the failure entirely:\n"
        + "\n".join(
            f"  {label}:{lineno}: "
            + ' '.join(t for t in argv if t != _UNPARSED)
            + (" [could not be tokenized; a reporter here cannot be trusted]"
               if _UNPARSED in argv else "")
            for label, lineno, argv in offenders
        )
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


def test_a_dynamic_argv_element_does_not_hide_the_call(tmp_path: Path) -> None:
    """`subprocess.run(["node", "--test", *files])` is an ordinary invocation.

    Requiring every element of the list to be a string constant dropped it from
    the scan entirely, so adding a non-TAP call in that form left the census and
    the reporter assertion green. Both directions are asserted: the dynamic part
    must not hide the call, and must not cost a compliant call its reporter
    either, which is the over-correction that would fail every such invocation.
    """
    bare = tmp_path / "bare.py"
    bare.write_text('subprocess.run(["node", "--test", *files])\n', encoding="utf-8")
    sites = _node_test_sites_in("f", bare, bare.read_text(encoding="utf-8"))
    assert sites, "a runner call with a dynamic file list must still be found"
    assert all(TAP_FLAG not in argv for _, _, argv in sites), (
        "this call has no reporter and must be reportable as an offender"
    )

    compliant = tmp_path / "compliant.py"
    compliant.write_text(
        'subprocess.run(["node", "--test", *files, "--test-reporter=tap"])\n',
        encoding="utf-8",
    )
    ok = _node_test_sites_in("f", compliant, compliant.read_text(encoding="utf-8"))
    assert any(TAP_FLAG in argv for _, _, argv in ok), (
        "a reporter spelled after the dynamic part must still count, or the rule "
        "rejects every invocation built this way"
    )


def test_a_fallback_tokenization_cannot_certify_a_call(tmp_path: Path) -> None:
    """Whitespace splitting does not know quoting, so it can hand back a bare
    `--test-reporter=tap` token lifted out of a quoted argument. Detecting the
    call that way is fine; CLEARING it is not, and clearing is the one direction
    this guard must never fail in."""
    command = 'node --test "unbalanced; echo --test-reporter=tap'
    argv = _tokenize(command)
    assert _UNPARSED in argv, "a fallback tokenization must mark itself untrusted"
    assert TAP_FLAG in argv, (
        "sanity: the bare flag really is present in the fallback tokens, which is "
        "exactly why its presence cannot be read as compliance"
    )
    sites = _scan("f", command)
    assert sites, "the call must still be FOUND; dropping it would be a silent skip"
    assert _offenders(sites), (
        "a call this scanner could not tokenize must not read as compliant"
    )

    clean = _tokenize("node --test --test-reporter=tap x")
    assert _UNPARSED not in clean, (
        "a command that tokenizes cleanly must still be certifiable, or the rule "
        "rejects every invocation in the repository"
    )


def test_a_folded_workflow_command_is_refused_not_ignored(tmp_path: Path) -> None:
    """A folded scalar joins its lines with no continuation marker, so a
    line-oriented scan finds no call at all. Refusing the shape keeps the
    silent-omission case from existing; the literal form must keep working."""
    folded = tmp_path / "folded.yml"
    folded.write_text(
        "jobs:\n  t:\n    steps:\n      - run: >\n          node\n          --test\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="folded command scalar"):
        _commands(folded, folded.read_text(encoding="utf-8"))

    literal = tmp_path / "literal.yml"
    literal.write_text(
        "jobs:\n  t:\n    steps:\n      - run: |\n          node --test x\n",
        encoding="utf-8",
    )
    sites = _node_test_sites_in("f", literal, literal.read_text(encoding="utf-8"))
    assert sites, (
        "the literal block is the form every command in this repository uses and "
        "must keep scanning, or the refusal above would break the whole workflow scan"
    )


def test_a_composed_argv_expression_is_read_as_one_command(tmp_path: Path) -> None:
    """`subprocess.run(["node"] + ["--test", *files])` is one invocation.

    Inspecting each literal on its own found neither token pair, so the call
    produced no site at all. This was the third finding in this function and all
    three were one class: a shape the scanner could not fully read reported as a
    file holding no call. Concatenation is therefore resolved as a whole.
    """
    bare = tmp_path / "composed.py"
    bare.write_text('subprocess.run(["node"] + ["--test", *files])\n', encoding="utf-8")
    sites = _node_test_sites_in("f", bare, bare.read_text(encoding="utf-8"))
    assert sites, "a composed argv must still name the call site"
    assert _offenders(sites), "it has no reporter, so it must not read as compliant"

    ok = tmp_path / "composed_ok.py"
    ok.write_text(
        'subprocess.run(["node", "--test"] + ["--test-reporter=tap"])\n',
        encoding="utf-8",
    )
    ok_sites = _node_test_sites_in("f", ok, ok.read_text(encoding="utf-8"))
    assert ok_sites and not _offenders(ok_sites), (
        "a reporter contributed by the other operand must count, or resolving the "
        "concatenation would fail every compliant call built this way"
    )

    # The absorbed literals must not also be counted alone, or the per file
    # census this guard depends on would drift upward on a refactor.
    assert len(sites) == 1, f"one command, not {len(sites)}: {sites}"


def test_every_command_surface_git_knows_about_is_in_scope() -> None:
    """The inventory is derived INDEPENDENTLY, from git rather than from the
    scanner's own rules, so the two cannot agree by sharing a mistake.

    An earlier version named five locations, which meant a `node --test` added in
    another app's package file or another root script was invisible while the
    pinned census and the offender test both stayed green. A list goes stale
    silently; this fails the day a surface appears outside the rules.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "Makefile", "justfile", "*/Makefile", "*/justfile",
         "package.json", "*/package.json"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split("\0")
    expected = {
        name for name in tracked
        if name and not any(part in EXCLUDED_DIRS for part in Path(name).parts)
    }
    assert expected, "git listed no command surfaces at all, so this proves nothing"

    scanned = {str(p.relative_to(REPO_ROOT)) for p in _candidate_files()}
    missing = sorted(expected - scanned)
    assert not missing, (
        "git tracks these command surfaces but the scanner does not look at them, so a "
        f"`node --test` added in one would be invisible: {missing}"
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
