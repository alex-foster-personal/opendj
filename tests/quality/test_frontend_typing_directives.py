"""Directive-comment (@ts-ignore / @ts-expect-error / @ts-nocheck) detection.

Split out of test_frontend_typing_gate.py (worker-rules' 600-line-per-Python-
file ratchet: five straight PR #731 review rounds, eight through twelve, grew
that one file to 672 lines). `any`-detection stayed there; every test pinning
directive-MATCHING mechanics -- case sensitivity, slash counts, suffix
tolerance, block-comment line/prefix rules, file-leading placement -- moved
here. Same scanner (apps/webui/frontend/scripts/ts-any-scan.mjs), same
qg._ts_escape_hatch_hits() entry point, same GUARD-10 marker.
"""

from __future__ import annotations

import pytest

from scripts import quality_gate as qg

# full-ci.yml's coverage lane `--ignore`s this file too, for the same
# node_modules/typescript dependency as test_frontend_typing_gate.py, so
# ci.yml's `quality` job is the only place these GUARD-10 tests are collected.
pytestmark = pytest.mark.requirement("GUARD-10")


def test_scanner_catches_directive_inside_svelte_script_block() -> None:
    """If directive detection moved to comment-token lexing loses .svelte
    coverage then a @ts-nocheck inside a real <script> block goes uncounted."""
    source = (
        '<script lang="ts">\n'
        "  // @ts-nocheck\n"
        "  const x = 1;\n"
        "</script>\n"
        "<div>{x}</div>\n"
    )
    assert qg._ts_escape_hatch_hits(source, suffix=".svelte") == [(2, "ts-nocheck")], (
        "if a .svelte <script> block's directive is not reported then moving "
        "directive detection off raw-line regex silently dropped .svelte "
        "coverage, so broken"
    )


def test_scanner_ignores_ts_nocheck_placed_after_real_code() -> None:
    """@ts-nocheck only disables checking for a whole file when it is the
    FILE-LEADING comment; verified against real tsc (round five of PR #731
    review): the same source with @ts-nocheck moved after a statement still
    reports every error normally, so a mid-file @ts-nocheck is inert and
    must not score as a suppression."""
    source = "const bad1: string = 1;\n// @ts-nocheck\nconst bad2: string = 2;\n"
    assert qg._ts_escape_hatch_hits(source) == [], (
        f"if {source!r} scores a ts-nocheck hit then a directive tsc never "
        f"applies (it only takes effect before any real code) blocks the "
        f"hard-zero gate on a harmless comment, so broken (got "
        f"{qg._ts_escape_hatch_hits(source)})"
    )


def test_scanner_catches_ts_nocheck_as_the_first_line_of_the_file() -> None:
    """The file-leading case must still count -- this is the one real tsc
    honors, verified the same way (the whole file is unchecked)."""
    source = "// @ts-nocheck\nconst bad1: string = 1;\nconst bad2: string = 2;\n"
    assert qg._ts_escape_hatch_hits(source) == [(1, "ts-nocheck")], (
        "if a file-leading @ts-nocheck stops counting then the gate is "
        "blind to the widest suppression in the language, so broken"
    )


def test_scanner_ignores_a_file_leading_block_comment_ts_nocheck() -> None:
    """@ts-nocheck only takes effect as a `//` single-line comment, never a
    `/* */` block comment, even when file-leading -- verified against
    pinned tsc 5.9.3 (round seven of PR #731 review): a file-leading
    `/* @ts-nocheck */` probe still reports its type error. TypeScript's own
    commentPragmas['ts-nocheck'] declares it SingleLine-only."""
    source = "/* @ts-nocheck */\nconst bad1: string = 1;\n"
    assert qg._ts_escape_hatch_hits(source) == [], (
        f"if a block-comment @ts-nocheck scores as a hit then a directive "
        f"tsc never honors in that form blocks the hard-zero gate on a "
        f"harmless comment, so broken (got {qg._ts_escape_hatch_hits(source)})"
    )


def test_scanner_catches_a_mixed_case_ts_nocheck() -> None:
    """tsc lowercases a pragma name before looking it up (addPragmaForMatch
    in TypeScript's own source: `const name = match[1].toLowerCase()`), so
    a file-leading `@TS-NOCHECK` disables checking exactly like the
    lowercase form -- verified against pinned tsc 5.9.3 (round eight of PR
    #731 review): a file-leading `// @TS-NOCHECK` probe reports no error at
    all. A case-sensitive scanner pattern reports no hit for the exact
    suppression the hard-zero gate exists to catch."""
    source = "// @TS-NOCHECK\nconst bad1: string = 1;\n"
    assert qg._ts_escape_hatch_hits(source) == [(1, "ts-nocheck")], (
        f"if a mixed-case @TS-NOCHECK scores as no hit then the gate is "
        f"blind to a suppression tsc actually applies, so broken (got "
        f"{qg._ts_escape_hatch_hits(source)})"
    )


def test_scanner_ignores_a_mixed_case_ts_ignore() -> None:
    """Unlike @ts-nocheck, @ts-ignore/@ts-expect-error are matched by tsc's
    own commentDirectiveRegExSingleLine/MultiLine, which carry no `i` flag
    -- verified against pinned tsc 5.9.3: a file with `// @TS-IGNORE` above
    a type error still reports that error, so the uppercase form is inert
    to tsc and must not score as a hit here either."""
    source = "const ok = 1;\n// @TS-IGNORE\nconst bad: string = 5;\n"
    assert qg._ts_escape_hatch_hits(source) == [], (
        f"if a mixed-case @TS-IGNORE scores as a hit then the gate blocks "
        f"on a comment tsc never treats as a suppression, so broken (got "
        f"{qg._ts_escape_hatch_hits(source)})"
    )


def test_scanner_ignores_a_four_slash_ts_ignore_lookalike() -> None:
    """TypeScript's own commentDirectiveRegExSingleLine is
    `/^\\/\\/\\/?\\s*@(ts-expect-error|ts-ignore)/` -- exactly two required
    slashes plus an OPTIONAL third, never a fourth -- verified against
    pinned tsc 5.9.3 (round eight of PR #731 review): a `//// @ts-ignore`
    probe above a type error still reports that error. A stripper that
    removes an unbounded run of leading `/`/`*` characters turns this
    harmless four-slash comment into a suppression tsc never honors."""
    source = "const ok = 1;\n//// @ts-ignore\nconst bad: string = 5;\n"
    assert qg._ts_escape_hatch_hits(source) == [], (
        f"if a four-slash `//// @ts-ignore` scores as a hit then the gate "
        f"blocks on a comment tsc never treats as a suppression, so broken "
        f"(got {qg._ts_escape_hatch_hits(source)})"
    )


def test_scanner_catches_a_triple_slash_ts_ignore() -> None:
    """The optional third slash IS real -- verified against pinned tsc
    5.9.3: a `/// @ts-ignore` probe above a type error reports no error, so
    this must keep scoring as a hit; the four-slash test above is the
    boundary this one sits right next to, not a reason to reject three."""
    source = "const ok = 1;\n/// @ts-ignore\nconst bad: string = 5;\n"
    assert qg._ts_escape_hatch_hits(source) == [(2, "ts-ignore")], (
        f"if a triple-slash `/// @ts-ignore` stops counting then the gate "
        f"is blind to a suppression tsc actually applies, so broken (got "
        f"{qg._ts_escape_hatch_hits(source)})"
    )


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("const y = 1;\n// @ts-ignoreXYZ\nconst x: number = 'no';\n", "ts-ignore"),
        ("const y = 1;\n// @ts-expect-errorXYZ\nconst x: number = 'no';\n", "ts-expect-error"),
    ],
)
def test_scanner_catches_a_suffixed_directive(source: str, rule: str) -> None:
    """tsc's own commentDirectiveRegExSingleLine/MultiLine
    (`/^\\/\\/\\/?\\s*@(ts-expect-error|ts-ignore)/`) carries no trailing
    word boundary or `$` anchor -- it is a pure PREFIX match -- verified
    against pinned tsc 5.9.3 (round nine of PR #731 review): a
    `// @ts-ignoreXYZ` probe above a type error still reports no error at
    all, i.e. the compiler treats the whole nonsense suffix as part of an
    honored directive. A `\\b`-anchored scanner pattern rejects that exact
    line, so the hard-zero gate would report zero while tsc is silently not
    checking the file."""
    assert rule in [r for _, r in qg._ts_escape_hatch_hits(source)], (
        f"if {source!r} does not score a {rule} hit then the gate is blind "
        f"to a suppression tsc actually applies, so broken (got "
        f"{qg._ts_escape_hatch_hits(source)})"
    )


def test_scanner_ignores_directive_text_on_a_non_final_block_comment_line() -> None:
    """TypeScript only checks the FINAL physical line of a multi-line block
    comment for a directive -- its own scanner tracks `lastLineStart` as
    whichever line is current and calls appendIfCommentDirective exactly
    once, after the comment closes, on `text.slice(lastLineStart, pos)`.
    Verified against pinned tsc 5.9.3 (round ten of PR #731 review): a
    `/* @ts-ignore\\n documentation */` probe, with the directive on the
    FIRST line, placed above a type error still reports that error, so a
    scanner that checks every line of a block comment counts a line tsc
    never applies."""
    source = "const y = 1;\n/* @ts-ignore\n documentation */\nconst x: number = 'no';\n"
    assert qg._ts_escape_hatch_hits(source) == [], (
        f"if {source!r} scores a ts-ignore hit then the gate blocks on a "
        f"block-comment line tsc never treats as a directive (only the "
        f"FINAL physical line counts), so broken (got "
        f"{qg._ts_escape_hatch_hits(source)})"
    )


def test_scanner_catches_directive_on_the_final_block_comment_line() -> None:
    """The mirror positive case, pinning the boundary from the other side:
    verified against pinned tsc 5.9.3, `/* documentation\\n @ts-ignore */`
    above a type error DOES suppress it, since @ts-ignore is now the last
    physical line of the block comment."""
    source = "const y = 1;\n/* documentation\n @ts-ignore */\nconst x: number = 'no';\n"
    assert qg._ts_escape_hatch_hits(source) == [(3, "ts-ignore")], (
        f"if {source!r} scores no ts-ignore hit then the gate is blind to "
        f"a suppression tsc actually applies, so broken (got "
        f"{qg._ts_escape_hatch_hits(source)})"
    )


@pytest.mark.parametrize(
    "source",
    [
        "// @ts-nocheck!\nconst bad1: string = 1;\n",
        "// @ts-nocheck.\nconst bad1: string = 1;\n",
        "// @ts-nocheck,\nconst bad1: string = 1;\n",
    ],
)
def test_scanner_ignores_ts_nocheck_followed_by_punctuation(source: str) -> None:
    """`singleLinePragmaRegEx` (`/^\\/\\/\\/?\\s*@([^\\s:]+)((?:[^\\S\\r\\n]|:).*)?$/m`)
    captures the WHOLE non-whitespace, non-colon run as the pragma name, so
    `@ts-nocheck!` is looked up as the literal name "ts-nocheck!", which has
    no commentPragmas entry -- verified against pinned tsc 5.9.3 (round
    twelve of PR #731 review): a file-leading `// @ts-nocheck!` probe still
    reports its type error. A `\\b`-anchored pattern matches here anyway,
    because punctuation is a word-boundary character too, so it rejects an
    inert comment and blocks the hard-zero gate on harmless code."""
    assert qg._ts_escape_hatch_hits(source) == [], (
        f"if {source!r} scores a ts-nocheck hit then the gate blocks on a "
        f"comment tsc never treats as a suppression, so broken (got "
        f"{qg._ts_escape_hatch_hits(source)})"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("// @ts-nocheck: legacy file\nconst bad1: string = 1;\n", [(1, "ts-nocheck")]),
        ("// @ts-nocheck some text after space\nconst bad1: string = 1;\n", [(1, "ts-nocheck")]),
    ],
)
def test_scanner_catches_ts_nocheck_followed_by_a_valid_delimiter(
    source: str, expected: list
) -> None:
    """The mirror positive case: whitespace and `:` ARE valid delimiters
    after the pragma name -- verified against pinned tsc 5.9.3, both probes
    report no error at all."""
    assert qg._ts_escape_hatch_hits(source) == expected, (
        f"if {source!r} scores no ts-nocheck hit then the gate is blind to "
        f"a suppression tsc actually applies, so broken (got "
        f"{qg._ts_escape_hatch_hits(source)})"
    )


def test_scanner_ignores_an_interleaved_block_comment_prefix() -> None:
    """TypeScript's multiline directive grammar
    (`commentDirectiveRegExMultiLine`, applied after `text.trimStart()`) only
    ever permits a contiguous run of `/`/`*` characters followed by
    whitespace, never whitespace interleaved BEFORE the slash/star run --
    verified against pinned tsc 5.9.3 (round twelve of PR #731 review): a
    `/* docs\\n / * @ts-ignore */` probe (space, then slash, then space,
    then star, on the final line) placed above a type error still reports
    it. A character-class stripper that treats whitespace and `/`/`*` as
    interchangeable in any order strips this whole prefix and counts a
    directive tsc never applies."""
    source = "const y = 1;\n/* docs\n / * @ts-ignore */\nconst x: number = 'no';\n"
    assert qg._ts_escape_hatch_hits(source) == [], (
        f"if {source!r} scores a ts-ignore hit then the gate blocks on an "
        f"interleaved comment prefix tsc never treats as a directive, so "
        f"broken (got {qg._ts_escape_hatch_hits(source)})"
    )


def test_scanner_catches_a_jsdoc_style_block_comment_prefix() -> None:
    """The mirror positive case: a leading-whitespace-then-star JSDoc
    continuation (` * @ts-ignore`) IS honored -- verified against pinned
    tsc 5.9.3 -- because TypeScript trims leading whitespace with
    `text.trimStart()` before applying the slash/star-run grammar, so this
    is not the same shape as the interleaved case above (there the `/`
    comes before any whitespace, which trimStart cannot fix)."""
    source = "const y = 1;\n/* docs\n * @ts-ignore */\nconst x: number = 'no';\n"
    assert qg._ts_escape_hatch_hits(source) == [(3, "ts-ignore")], (
        f"if {source!r} scores no ts-ignore hit then the gate is blind to "
        f"a suppression tsc actually applies, so broken (got "
        f"{qg._ts_escape_hatch_hits(source)})"
    )


def test_scanner_catches_a_trailing_same_line_ts_ignore() -> None:
    """@ts-ignore/@ts-expect-error apply by comment LINE regardless of
    leading/trailing position -- verified against real tsc (round five of
    PR #731 review): `const ok = 1; // @ts-ignore` still suppresses the
    following line's error, so this must keep counting as a hit rather than
    being excluded on the theory that a trailing comment cannot apply."""
    source = "const ok = 1; // @ts-ignore\nconst bad: string = 5;\n"
    assert qg._ts_escape_hatch_hits(source) == [(1, "ts-ignore")], (
        f"if a trailing same-line @ts-ignore stops counting then the gate "
        f"is blind to a suppression tsc actually applies, so broken (got "
        f"{qg._ts_escape_hatch_hits(source)})"
    )


def test_scanner_ignores_a_directive_lookalike_in_svelte_markup() -> None:
    """Directives are only meaningful inside a <script> block; markup text
    that merely mentions one must not score, the same guarantee as prose."""
    source = (
        '<script lang="ts">\n'
        "  const x = 1;\n"
        "</script>\n"
        "<!-- @ts-ignore this is just documentation copy -->\n"
        "<div>{x}</div>\n"
    )
    assert qg._ts_escape_hatch_hits(source, suffix=".svelte") == [], (
        "if the markup comment scores as a directive hit then non-script "
        "content is being lexed as TypeScript, so broken"
    )


@pytest.mark.parametrize(
    "source",
    [
        # Round four of PR #731 review: a directive mid-sentence inside a
        # REAL comment (not a string literal, which round three already
        # covers). TypeScript's own commentDirectiveRegExSingleLine/
        # MultiLine anchor the directive to the start of the comment, so
        # prose that merely mentions one is not a suppression to tsc and
        # must not be one to this gate either.
        "// Never use @ts-ignore here, it's a code smell.",
        "// Docs: @ts-expect-error is for known-broken upstream types.",
        "/* Some files still need @ts-nocheck, but this one doesn't. */",
        "/**\n * Never use @ts-ignore here.\n */",
    ],
)
def test_scanner_ignores_a_directive_mentioned_mid_comment(source: str) -> None:
    """If a directive is matched anywhere in a comment then prose that
    merely discusses one scores as a suppression tsc never applied."""
    assert qg._ts_escape_hatch_hits(source) == [], (
        f"if {source!r} scores as a directive hit then the match is not "
        "anchored to the start of the comment the way tsc's own directive "
        "regex is, so broken"
    )


def test_scanner_ignores_a_directive_inside_an_interpolated_template_tail() -> None:
    """A context-free `ts.createScanner` call is not driven through
    `reScanTemplateToken`, so after the `}` closing a `${...}` interpolation
    it tokenizes the rest of the template literal as a fresh comment/code
    stream instead of a template-string tail (round thirteen of PR #731
    review, Codex P2/BLOCKING at ts-any-scan.mjs:117). `` `Never write ${1}
    // @ts-ignore here` `` therefore scanned as `SingleLineCommentTrivia`
    containing a real directive -- verified against pinned tsc 5.9.3: the
    same source above a real type error still reports it, so the compiler
    never treats this template's contents as a suppression at all."""
    source = "const help = `Never write ${1} // @ts-ignore here`;\nconst bad: string = 5;\n"
    assert qg._ts_escape_hatch_hits(source) == [], (
        f"if {source!r} scores a ts-ignore hit then an ordinary UI string "
        f"blocks the hard-zero gate on a suppression tsc never applies, so "
        f"broken (got {qg._ts_escape_hatch_hits(source)})"
    )


def test_scanner_ignores_an_expect_error_inside_an_interpolated_template_tail() -> None:
    """The mirror case for @ts-expect-error, same root cause as above."""
    source = "const h = `x ${1} // @ts-expect-error z`;\nconst bad: string = 5;\n"
    assert qg._ts_escape_hatch_hits(source) == [], (
        f"if {source!r} scores an expect-error hit then the same template-"
        f"tail misclassification is still live, so broken (got "
        f"{qg._ts_escape_hatch_hits(source)})"
    )


def test_directives_still_count_inside_comments() -> None:
    """If a directive inside a comment stopped counting the gate would see none."""
    # @ts-ignore and friends are ONLY ever legal inside a comment, so they are
    # matched on every line unconditionally, unlike `any` which is parsed.
    assert qg._ts_escape_hatch_hits("// @ts-ignore") == [(1, "ts-ignore")], (
        "if a directive inside a comment stops counting then the gate is "
        "blind to the widest suppression in the language, so broken"
    )
