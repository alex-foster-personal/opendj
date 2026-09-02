"""The frontend's type cleanliness has to be GATED, not merely achieved.

On Tue 1 Sep 2026 the strong-typing audit measured the frontend at zero `any`,
zero `@ts-ignore` and zero tsc errors. Nothing defended that number: three
compiler flags that were free to turn on (noImplicitReturns,
noFallthroughCasesInSwitch, exactOptionalPropertyTypes) were off, and no metric
counted the escape hatches, so the first PR to reach for one would land green.
Issue #724 turns the flags on and adds `frontend.ts_escape_hatches` as a hard
zero; this module is what stops either half from being quietly undone.

`any`-detection is AST-based (apps/webui/frontend/scripts/ts-any-scan.mjs),
not regex, after two rounds of review on PR #731 found gaps in token-adjacency
matching: round one missed union/intersection/operator position (`string |
any`), round two -- after that was fixed -- still missed `type X = [any]`,
`interface X { values: readonly any[] }` and `type X = (any)`, plus false
positives inside string/template literals and trailing comments (`"cast it as
any"`, `// Returns: any value`). Enumerating adjacency patterns cannot close a
grammar; a real parse walks every syntactic position at once and structurally
cannot match inside a string or a comment.

A third round found the same defect twice more: `@ts-ignore`/
`@ts-expect-error`/`@ts-nocheck` were still matched with a raw-line regex, so
`const help = "Never use @ts-ignore here";` counted as a suppression; they are
now found by tokenizing and keeping only comment trivia, same fix as `any`.
And `.svelte` files were only ever extracted as `<script>` block text, so an
`any` cast written directly in markup (`on:click={(e) => e.target as any}`,
a pattern already live in the tree) scored as clean; `.svelte` files are now
parsed whole with `svelte/compiler`, whose AST covers script blocks and every
markup expression in one walk.

Regression lines:
  - if tsconfig.json drops noImplicitReturns, noFallthroughCasesInSwitch or
    exactOptionalPropertyTypes then the ratchet is undone, so broken
  - if frontend.ts_escape_hatches leaves HARD_ZERO then a baseline allowance
    could absorb a suppression, so broken
  - if the scanner misses @ts-ignore, @ts-expect-error or any explicit `any`
    position then it is reporting a false clean, so broken
  - if the scanner counts "has anything" or "as anything" then it fails on
    prose and gets switched off, so broken
  - if a parse failure renders as zero hits then a broken measurement reads
    as a clean tree, so broken
  - if .svelte script-block extraction only ever reads the first block then a
    module-context block landing first hides the instance block's `any`, so
    broken
  - if a directive is matched by raw-line regex then a string literal
    mentioning one scores as a suppression, so broken
  - if .svelte markup expressions are never parsed then an `any` cast written
    directly in a template (not a <script> block) is invisible, so broken
  - if the live frontend tree measures more than zero escape hatches then a
    suppression has landed, so broken

Directive-matching mechanics (@ts-ignore/@ts-expect-error/@ts-nocheck case
sensitivity, slash counts, suffix tolerance, block-comment line/prefix rules)
live in test_frontend_typing_directives.py -- split out to keep this file
under worker-rules' 600-line-per-Python-file ratchet floor after five
straight review rounds of test additions here.
"""

from __future__ import annotations

import pytest

from scripts import quality_gate as qg

# The tsconfig-flag and hard-zero-metric checks moved to
# test_frontend_typing_ratchet.py (round four of PR #731 review): they need
# no node subprocess, so they stay collected by full-ci.yml's coverage lane,
# which `--ignore`s this whole file for its node_modules/typescript
# dependency. Marked here too so every AST-scanner regression in this file
# also traces to GUARD-10 in ci.yml's `quality` job, which does run it.
pytestmark = pytest.mark.requirement("GUARD-10")


# Every syntax that turns the compiler off. `any` alone can occupy any
# syntactic position a type can, and a scanner that enumerates positions by
# regex loses to the grammar; the AST walk catches all of them the same way
# (found in review of PR #731, two rounds).
@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("// @ts-ignore\nfoo();", "ts-ignore"),
        ("// @ts-expect-error - server lies\nfoo();", "ts-expect-error"),
        ("// @ts-nocheck\nfoo();", "ts-nocheck"),
        # A block-comment `/* @ts-ignore */` still works (verified against
        # pinned tsc 5.9.3): commentDirectiveRegExMultiLine covers it. Round
        # seven of PR #731 review found `/* @ts-nocheck */` does NOT --
        # `commentPragmas['ts-nocheck']` is declared SingleLine-only in
        # TypeScript's own source, confirmed empirically the same way (a
        # file-leading block-comment probe still reports its error). See
        # the "ignores" parametrization below for that negative case.
        ("/* @ts-ignore */\nfoo();", "ts-ignore"),
        ("const x = payload as any;", "any"),
        ("const x = (payload as any).deck;", "any"),
        ("\t\tfn(row as any)", "any"),
        ("let payload: any;", "any"),
        ("function pick(row: any) {}", "any"),
        ("try {\n} catch (err: any) {}", "any"),
        ("function load(): any {}", "any"),
        ("const rows: any[] = [];", "any"),
        ("type Payload = any;", "any"),
        ("const x = <any>payload;", "any"),
        ("let p: Promise<any>;", "any"),
        ("const m = new Map<any, any>();", "any"),
        # Union/intersection/operator position (round one of PR #731 review):
        # the original four rules only matched `any` immediately adjacent to
        # a token, so these all landed green.
        ("type Payload = string | any;", "any"),
        ("const z = <string | any>payload;", "any"),
        ("type W = any & OtherThing;", "any"),
        ("type K = keyof any;", "any"),
        ("const y = value satisfies any;", "any"),
        ("function f<T extends any>(x: T) {}", "any"),
        # Tuple/array/paren position (round two of PR #731 review, after
        # round one shipped): every adjacency-regex fix still left these
        # uncaught, which is why detection moved to a real parse instead of
        # a sixth regex.
        ("type X = [any];", "any"),
        ("interface Y { values: readonly any[] }", "any"),
        ("type Z = (any);", "any"),
    ],
)
def test_scanner_catches_every_escape_hatch(source: str, rule: str) -> None:
    """If a suppression form is missed then the gate reports a false clean."""
    hits = qg._ts_escape_hatch_hits(source)
    assert rule in [r for _, r in hits], (
        f"if {source!r} does not scan as a {rule} hit then the gate is blind "
        f"to it, so broken (got {hits})"
    )


def test_scanner_catches_any_inside_a_svelte_script_block() -> None:
    """If .svelte extraction misses a block then an `any` there is invisible."""
    source = (
        '<script context="module" lang="ts">\n'
        "  export const mod = 1;\n"
        "</script>\n"
        '<script lang="ts">\n'
        "  const x: any = 1;\n"
        "</script>\n"
        "<div>{x}</div>\n"
    )
    hits = qg._ts_escape_hatch_hits(source, suffix=".svelte")
    assert (5, "any") in hits, (
        f"if a `<script>` block's `any` is not reported at its real line "
        f"then the svelte extraction dropped a block, so broken (got {hits})"
    )


@pytest.mark.parametrize(
    "source",
    [
        "/** True when the deck has anything loaded. */",
        "const x = value as anything;",
        "/** Cast as anybody would. */",
        "const anyMode = mode;",
        "// as-any is discussed in the docs",
        # Prose that would match an explicit-any rule if comment-only lines
        # were scanned for them. A gate that reds on a doc comment is a gate
        # somebody deletes.
        "// Returns: any of the following four lanes.",
        "/**\n * @param mode - any of 'sweet' | 'half' | 'mid' | 'far'\n */\n"
        "function f(mode: string) {}",
        "/* Accepts: any string the daemon sends. */",
        # String/template literals and trailing comments (round two of PR
        # #731 review): a token-adjacency regex cannot tell these from a
        # real type position because it never lexes the source.
        'const help = "cast it as any before calling";',
        "const n = 1; // Returns: any value",
        "const t = `this is a template with any inside`;",
        # Round three of PR #731 review: a directive string, not a directive
        # comment. A raw-line regex cannot tell these apart; a lexer can,
        # because a string literal never tokenizes as comment trivia.
        'const help = "Never use @ts-ignore here";',
        'const msg = `@ts-nocheck is not a real pragma inside a template`;',
    ],
)
def test_scanner_ignores_prose_that_merely_contains_the_letters(source: str) -> None:
    """If prose scores as a violation then the gate fails on comments."""
    assert qg._ts_escape_hatch_hits(source) == [], (
        f"if {source!r} scores as an escape hatch then the word boundaries are "
        "wrong and the gate fails on English, so broken"
    )


def test_scanner_ignores_a_template_markup_comment_mentioning_any() -> None:
    """If .svelte markup outside <script> is parsed then a template HTML
    comment mentioning `any` would falsely score as an escape hatch."""
    source = (
        '<script lang="ts">\n'
        "  const x = 1;\n"
        "</script>\n"
        "<!-- Renders: any row the pane holds -->\n"
        "<div>{x}</div>\n"
    )
    assert qg._ts_escape_hatch_hits(source, suffix=".svelte") == [], (
        "if the markup comment scores as a hit then non-script content is "
        "being parsed as TypeScript, so broken"
    )


def test_scanner_catches_any_cast_inside_svelte_markup() -> None:
    """If markup expressions are never parsed then an `any` cast in a
    template attribute (not a <script> block) is invisible to the gate."""
    source = (
        '<script lang="ts">\n'
        "  let count = 0;\n"
        "</script>\n"
        '<button on:click={(e) => { const t = e.target as any; count++; }}>\n'
        "  {count}\n"
        "</button>\n"
    )
    hits = qg._ts_escape_hatch_hits(source, suffix=".svelte")
    assert ("any" in [rule for _, rule in hits]) is True, (
        f"if a markup-embedded `as any` cast does not score as a hit then "
        f"svelte extraction still only reads <script> text, so broken (got {hits})"
    )


def test_scanner_catches_any_inside_a_svelte_generics_declaration() -> None:
    """If the `generics` script attribute is never parsed then an `any`
    bound written there (`<script lang="ts" generics="T extends any">`) is
    invisible to the gate even though svelte-check compiles and checks it."""
    source = (
        '<script lang="ts" generics="T extends any">\n'
        "  export let value: T;\n"
        "</script>\n"
        "<div>{value}</div>\n"
    )
    hits = qg._ts_escape_hatch_hits(source, suffix=".svelte")
    assert ("any" in [rule for _, rule in hits]) is True, (
        f"if a `generics` attribute's `any` bound does not score as a hit "
        f"then the scanner only walks script content and markup, missing "
        f"the third place checked TypeScript lives in a component, so "
        f"broken (got {hits})"
    )


def test_scanner_ignores_a_clean_svelte_generics_declaration() -> None:
    """A `generics` bound that never mentions `any` must not score."""
    source = (
        '<script lang="ts" generics="T extends string">\n'
        "  export let value: T;\n"
        "</script>\n"
        "<div>{value}</div>\n"
    )
    assert qg._ts_escape_hatch_hits(source, suffix=".svelte") == [], (
        "if a clean `generics` bound scores as a hit then the parse of the "
        "attribute text is wrong, so broken"
    )


def test_scanner_catches_any_inside_a_svelte_script_jsdoc_type_tag() -> None:
    """The standalone `.js` JSDoc gap (round ten) was fixed by walking
    `node.jsDoc` in `_anyHits`, but `_svelteHits` never routes script text
    through `_anyHits` at all -- it only walks the svelte/compiler ESTree,
    whose comments are raw text, never `TSAnyKeyword` nodes (round eleven
    of PR #731 review). A checked JS component with a JSDoc `@type {any}`
    therefore scored zero. Each svelte script block's raw text must also go
    through `_anyHits`, the same TS parser used for standalone `.js`."""
    source = (
        '<script>\n'
        "  // @ts-check\n"
        "  /** @type {any} */\n"
        "  let value;\n"
        "</script>\n"
        "<div>{value}</div>\n"
    )
    hits = qg._ts_escape_hatch_hits(source, suffix=".svelte")
    assert "any" in [rule for _, rule in hits], (
        f"if a JSDoc @type {{any}} inside a <script> block scores no any "
        f"hit then the gate is blind to it, so broken (got {hits})"
    )


def test_scanner_catches_any_inside_a_js_file_jsdoc_type_tag() -> None:
    """`ts.forEachChild` does not descend into a node's `.jsDoc` array (a
    documented TypeScript compiler-API trap) -- verified against pinned tsc
    5.9.3 and by dumping the raw parse tree: `/** @type {any} */` on a
    checked .js file produces a real AnyKeyword node under
    `node.jsDoc[0].tags[0].typeExpression`. The project scan includes `.js`
    files (round ten of PR #731 review), where a JSDoc `@type {any}` is the
    only way to spell this escape hatch, so a walk that only calls
    forEachChild is blind to it."""
    source = "// @ts-check\n/** @type {any} */\nlet x;\n"
    hits = qg._ts_escape_hatch_hits(source, suffix=".js")
    assert "any" in [r for _, r in hits], (
        f"if {source!r} scores no any hit then a JSDoc-typed escape hatch "
        f"in a .js file is invisible to the hard-zero gate, so broken (got {hits})"
    )


def test_scanner_catches_a_jsdoc_wildcard_type_tag_in_a_js_file() -> None:
    """`/** @type {*} */` is JSDoc's own spelling of `any` -- verified
    against pinned tsc 5.9.3 (round fourteen of PR #731 review, Codex
    P1/BLOCKING): a checked .js file with `payload.nonexistent()` reports no
    error when `payload` carries this annotation, but does report one
    without it. TypeScript represents the wildcard as its own node kind,
    `JSDocAllType`, never `AnyKeyword` -- confirmed by dumping the raw parse
    tree -- so a walk that only checks for `AnyKeyword` reaches the node
    (jsDoc is already walked, round ten) but never records it as a hit."""
    source = "// @ts-check\n/** @type {*} */\nlet x;\n"
    hits = qg._ts_escape_hatch_hits(source, suffix=".js")
    assert "any" in [r for _, r in hits], (
        f"if {source!r} scores no any hit then a JSDoc wildcard escape "
        f"hatch in a .js file is invisible to the hard-zero gate, so broken "
        f"(got {hits})"
    )


def test_scanner_catches_a_jsdoc_wildcard_type_tag_in_a_svelte_js_script() -> None:
    """The mirror case for a JavaScript (not TypeScript) Svelte `<script>`
    block, the other place Codex's finding named: `_jsDocAnyLines` already
    routes every script block's raw text through `_anyHits`, so the same
    `JSDocAllType` fix covers it without a separate code path."""
    source = (
        '<script>\n'
        "  // @ts-check\n"
        "  /** @type {*} */\n"
        "  let value;\n"
        "</script>\n"
        "<div>{value}</div>\n"
    )
    hits = qg._ts_escape_hatch_hits(source, suffix=".svelte")
    assert "any" in [rule for _, rule in hits], (
        f"if a JSDoc @type {{*}} inside a <script> block scores no any hit "
        f"then the gate is blind to it, so broken (got {hits})"
    )


def test_scanner_catches_a_jsdoc_question_mark_type_tag_in_a_js_file() -> None:
    """`/** @type {?} */` (a BARE question mark, no type after it) is a
    second JSDoc any-like spelling, distinct from the `*` wildcard fixed
    above -- verified against pinned tsc 5.9.3 (round fifteen of PR #731
    review, Codex P1/BLOCKING): a checked .js file with
    `payload.nonexistent()` reports no error when `payload` carries this
    annotation. TypeScript represents it as `JSDocUnknownType`, a third node
    kind neither `AnyKeyword` nor `JSDocAllType` covers."""
    source = "// @ts-check\n/** @type {?} */\nlet x;\n"
    hits = qg._ts_escape_hatch_hits(source, suffix=".js")
    assert "any" in [r for _, r in hits], (
        f"if {source!r} scores no any hit then a JSDoc question-mark escape "
        f"hatch in a .js file is invisible to the hard-zero gate, so broken "
        f"(got {hits})"
    )


def test_scanner_catches_a_jsdoc_question_mark_type_tag_in_a_svelte_js_script() -> None:
    """The mirror case for a JavaScript Svelte `<script>` block, the other
    place Codex's finding named."""
    source = (
        '<script>\n'
        "  // @ts-check\n"
        "  /** @type {?} */\n"
        "  let value;\n"
        "</script>\n"
        "<div>{value}</div>\n"
    )
    hits = qg._ts_escape_hatch_hits(source, suffix=".svelte")
    assert "any" in [rule for _, rule in hits], (
        f"if a JSDoc @type {{?}} inside a <script> block scores no any hit "
        f"then the gate is blind to it, so broken (got {hits})"
    )


def test_scanner_ignores_a_jsdoc_nullable_type_with_a_real_type() -> None:
    """The mirror negative case, pinning the boundary from the other side:
    `?string` (a question mark immediately followed by a real type, meaning
    "string or null") is a DIFFERENT node kind, `JSDocNullableType`, not
    `JSDocUnknownType` -- confirmed by dumping the raw parse tree. Verified
    against pinned tsc 5.9.3 that this is a real, narrow type, not an escape
    hatch: the same nonexistent-property probe still errors on it. Flagging
    `JSDocNullableType` too would fail the hard-zero gate on legitimate
    nullable JSDoc types."""
    source = "// @ts-check\n/** @type {?string} */\nlet x;\n"
    hits = qg._ts_escape_hatch_hits(source, suffix=".js")
    assert "any" not in [r for _, r in hits], (
        f"if {source!r} scores an any hit then a real nullable JSDoc type "
        f"is being treated as an escape hatch, so broken (got {hits})"
    )


@pytest.mark.parametrize(
    "source",
    [
        # lib/rb/midi/webmidi.svelte.ts declares a boolean flag literally
        # named `any`. It must score as an identifier reference, never an
        # `AnyKeyword` type node, or this real, live variable becomes a
        # false positive.
        "let any = false;",
        "any = true;",
        "if (!any && _ledTimer !== null) {}",
        "if (any || fallbackFlag) {}",
    ],
)
def test_scanner_ignores_a_boolean_flag_literally_named_any(source: str) -> None:
    """If a variable named `any` counts as a hit then the gate fails on real code."""
    assert qg._ts_escape_hatch_hits(source) == [], (
        f"if {source!r} scores as an escape hatch then the parser cannot "
        "tell a type keyword from an identifier, so broken"
    )


def test_scanner_reports_the_line_number_of_each_hit() -> None:
    """If line numbers are wrong then the offender detail cannot be acted on."""
    source = "const ok = 1;\n// @ts-ignore\nconst bad = x as any;\n"
    assert qg._ts_escape_hatch_hits(source) == [(2, "ts-ignore"), (3, "any")], (
        "if the scanner loses line numbers or ordering then its detail column "
        "names the wrong place, so broken"
    )


# Hand-written, compiler-checked files that _frontend_files() would have
# missed because they live outside src/ (found in review of PR #731): the
# scan reused the src/-only file list, so an `any` planted in config or
# tooling code was invisible to the hard-zero gate.
_FILES_OUTSIDE_SRC = (
    qg.FRONTEND / "vite.config.ts",
    qg.FRONTEND / "webui-port-config.ts",
    qg.FRONTEND / ".storybook" / "main.ts",
    qg.FRONTEND / "tests" / "e2e" / "playwright.performance.config.ts",
)


@pytest.mark.parametrize("path", _FILES_OUTSIDE_SRC)
def test_project_scan_covers_hand_written_files_outside_src(path: object) -> None:
    """If a checked file outside src/ is missing then it is unprotected."""
    files = qg._ts_project_files()
    assert path in files, (
        f"if {path} is absent from _ts_project_files() then the escape-hatch "
        "gate does not cover it, so broken"
    )


def test_project_scan_excludes_generated_and_vendored_directories() -> None:
    """If node_modules/.svelte-kit are walked then the scan is huge and noisy."""
    files = qg._ts_project_files()
    assert not any("node_modules" in p.parts for p in files), (
        "if node_modules is walked then the scan is vendored code, not "
        "hand-written frontend sources, so broken"
    )
    assert not any(".svelte-kit" in p.parts for p in files), (
        "if .svelte-kit is walked then the scan is generated output, not "
        "hand-written frontend sources, so broken"
    )


def test_live_frontend_tree_has_no_escape_hatches() -> None:
    """If a suppression has landed anywhere in the frontend project then the
    hard zero is breached."""
    count, detail = qg._ts_escape_hatches()
    assert count == 0, (
        f"if the frontend project carries a compiler suppression then the "
        f"hard zero is breached, so broken: {detail}"
    )
