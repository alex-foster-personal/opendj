/**
 * Pin 5503680a4e0f (task 2, remaining half): a central, unit-tested WCAG
 * contrast validator any colour scheme must pass, so a user-authored scheme
 * can never ship unreadable.
 *
 * Regression lines:
 * - if relativeLuminance/contrastRatio drift from the WCAG formula then
 *   every downstream verdict is silently wrong
 * - if parseColorTokens misreads theme.css's own declaration blocks then the
 *   shipped-palette regression test is checking the wrong colours entirely
 * - if validateScheme reports a pass for a pairing that is actually below
 *   its threshold then the one thing this module exists to catch is broken
 * - if the repo's ACTUAL shipped dark or light scheme fails its own stated
 *   thresholds then the palette (not the threshold) must be fixed
 */

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { before, test } from "node:test";
import { fileURLToPath } from "node:url";

import { loadTypeScriptModule } from "./load-typescript.mjs";

let cc;
before(async () => {
  cc = await loadTypeScriptModule("src/lib/rb/color-contrast.ts");
});

// ----- relativeLuminance / contrastRatio -----------------------------------
test("contrastRatio: black on white is the maximum, 21:1", () => {
  assert.equal(Math.round(cc.contrastRatio("#000000", "#ffffff") * 100) / 100, 21);
});

test("contrastRatio: a colour against itself is the minimum, 1:1", () => {
  assert.equal(cc.contrastRatio("#7a8088", "#7a8088"), 1);
});

test("contrastRatio: fg/bg order never matters", () => {
  assert.equal(
    cc.contrastRatio("#123456", "#abcdef"),
    cc.contrastRatio("#abcdef", "#123456"),
  );
});

test("contrastRatio: a known WCAG-documented pair matches published figure", () => {
  // #767676 on #ffffff is WCAG's own worked example for the 4.5:1 floor.
  const ratio = cc.contrastRatio("#767676", "#ffffff");
  assert.ok(Math.abs(ratio - 4.54) < 0.02, `expected ~4.54, got ${ratio}`);
});

// ----- parseColorTokens -----------------------------------------------------
const SAMPLE_CSS = `
.perf-root {
	--rb-bg: #0d0f12;
	--rb-text: #c8cdd2;
	--rb-accent-glow: rgba(47, 111, 214, 0.55);
}

html[data-theme='light'] .perf-root {
	--rb-bg: #f7f3eb;
	--rb-text: #27241f;
}
`;

test("parseColorTokens reads only the hex custom properties in ONE selector's block", () => {
  const dark = cc.parseColorTokens(SAMPLE_CSS, ".perf-root");
  assert.deepEqual(dark, { "rb-bg": "#0d0f12", "rb-text": "#c8cdd2" });
});

test("parseColorTokens does not bleed into the next block with the same-looking selector", () => {
  const light = cc.parseColorTokens(SAMPLE_CSS, "html[data-theme='light'] .perf-root");
  assert.deepEqual(light, { "rb-bg": "#f7f3eb", "rb-text": "#27241f" });
});

test("parseColorTokens skips a non-hex declaration (rgba) rather than mis-parsing it", () => {
  const dark = cc.parseColorTokens(SAMPLE_CSS, ".perf-root");
  assert.equal(dark["rb-accent-glow"], undefined);
});

test("parseColorTokens raises on a selector that is not in the stylesheet at all", () => {
  assert.throws(() => cc.parseColorTokens(SAMPLE_CSS, ".no-such-block"), /selector/);
});

// ----- validateScheme --------------------------------------------------------
test("validateScheme: a scheme that meets every threshold reports no violations", () => {
  const tokens = { "rb-text": "#000000", "rb-bg": "#ffffff" };
  const pairings = [{ fg: "rb-text", bg: "rb-bg", level: "body", label: "primary text" }];
  assert.deepEqual(cc.validateScheme(tokens, pairings), []);
});

test("validateScheme: a pairing below its body threshold IS reported, with the numbers", () => {
  // #777777 on #808080 measures well under 4.5:1 - a deliberately broken
  // scheme, to prove the validator actually catches a violation rather than
  // rubber-stamping everything (mutation check: this must fail red first).
  const tokens = { "rb-text": "#777777", "rb-bg": "#808080" };
  const pairings = [{ fg: "rb-text", bg: "rb-bg", level: "body", label: "primary text" }];
  const violations = cc.validateScheme(tokens, pairings);
  assert.equal(violations.length, 1);
  assert.equal(violations[0].pairing.label, "primary text");
  assert.equal(violations[0].required, 4.5);
  assert.ok(violations[0].ratio < 4.5);
});

test("validateScheme: a non-text/large pairing only needs 3:1, not 4.5:1", () => {
  // ~3.8:1 - fails body text, passes a non-text UI indicator.
  const tokens = { "rb-accent": "#2f6fd6", "rb-bg": "#0d0f12" };
  const pairings = [
    { fg: "rb-accent", bg: "rb-bg", level: "non-text", label: "accent indicator" },
  ];
  assert.deepEqual(cc.validateScheme(tokens, pairings), []);
});

test("validateScheme: a missing token in the scheme is reported, not silently skipped", () => {
  const tokens = { "rb-bg": "#0d0f12" };
  const pairings = [
    { fg: "rb-text", bg: "rb-bg", level: "body", label: "primary text" },
  ];
  const violations = cc.validateScheme(tokens, pairings);
  assert.equal(violations.length, 1);
  assert.match(violations[0].reason ?? "", /rb-text/);
});

test("describeViolations renders a one-line-per-violation report", () => {
  const tokens = { "rb-text": "#777777", "rb-bg": "#808080" };
  const pairings = [{ fg: "rb-text", bg: "rb-bg", level: "body", label: "primary text" }];
  const violations = cc.validateScheme(tokens, pairings);
  const report = cc.describeViolations(violations);
  assert.match(report, /primary text/);
  assert.match(report, /4\.5/);
});

// ----- the shipped palette itself -------------------------------------------
const THEME_CSS = readFileSync(
  fileURLToPath(new URL("../../src/lib/rb/theme.css", import.meta.url)),
  "utf8",
);

test("the shipped DARK scheme passes every stated threshold", () => {
  const tokens = cc.parseColorTokens(THEME_CSS, ".perf-root");
  const violations = cc.validateScheme(tokens, cc.PAIRINGS);
  assert.deepEqual(violations, [], cc.describeViolations(violations));
});

test("the shipped LIGHT scheme passes every stated threshold", () => {
  const tokens = cc.parseColorTokens(THEME_CSS, "html[data-theme='light'] .perf-root");
  const violations = cc.validateScheme(tokens, cc.PAIRINGS);
  assert.deepEqual(violations, [], cc.describeViolations(violations));
});

// ----- PAIR-08: skins and text on the accent fill ----------------------------
// the maintainer, Tue 6 Oct 2026: "capture should be dark font ofc - contrast rules
// should have aguht [caught] it". Two gaps let a white label ship on Gothic's pale
// accent: the validator only measured the two base schemes, never a skin
// layered over them, and a component's hardcoded `color: #fff` on
// `--rb-accent` was never a token pairing at all.
const SKIN_SELECTORS = {
  "mono-dev (Gothic)": "html[data-skin='mono-dev'] .perf-root",
  "light": "html[data-skin='light'] .perf-root",
};

for (const [skin, sel] of Object.entries(SKIN_SELECTORS)) {
  test(`PAIR-08 the shipped ${skin} skin, layered over the base scheme, passes every threshold`, () => {
    const tokens = { ...cc.parseColorTokens(THEME_CSS, ".perf-root"), ...cc.parseColorTokens(THEME_CSS, sel) };
    // Presence first: the on-accent pairing must be declared and measured.
    assert.ok(
      cc.PAIRINGS.some((p) => p.fg === "rb-on-accent" && p.bg === "rb-accent" && p.level === "body"),
      "no body pairing pins rb-on-accent on rb-accent",
    );
    assert.match(tokens["rb-on-accent"] ?? "", /^#[0-9a-f]{6}$/i, `${skin} lacks rb-on-accent`);
    const violations = cc.validateScheme(tokens, cc.PAIRINGS);
    assert.deepEqual(violations, [], cc.describeViolations(violations));
  });
}

test("PAIR-08 mutation: white text on Gothic's pale accent is reported", () => {
  const tokens = {
    ...cc.parseColorTokens(THEME_CSS, ".perf-root"),
    ...cc.parseColorTokens(THEME_CSS, SKIN_SELECTORS["mono-dev (Gothic)"]),
    "rb-on-accent": "#ffffff",
  };
  const violations = cc.validateScheme(tokens, cc.PAIRINGS);
  assert.ok(
    violations.some((v) => v.pairing.fg === "rb-on-accent" && v.ratio < 4.5),
    cc.describeViolations(violations) || "white on #d6d6d6 (1.45:1) was not reported",
  );
});

// Every CSS rule that paints an --rb-accent fill AND sets a text colour must
// take that colour from --rb-on-accent, the token the pairing above measures
// per skin. A literal there (#fff, a dark hex, --rb-text) is unmeasured.
const SRC_ROOT = fileURLToPath(new URL("../../src", import.meta.url));
// TrackTable.svelte is a single-owner hotspot this lane may not edit
// (orders board #5638); its one hit is named here so it stays visible.
const ACCENT_TEXT_ALLOWLIST = new Set(["lib/components/rb/browser/TrackTable.svelte .load-confirm-yes:hover"]);

// A state rule (`.x:hover { background: var(--rb-accent) }`) often sets no
// colour of its own and paints the colour its base rule (`.x`) declares, so a
// fill rule with no colour is resolved through its base selector before it
// can count as text-free (Codex on #5727: StripWaveform's .play-hint).
const STATE_PSEUDO = /(?::(?:hover|active|focus|focus-visible|focus-within))+$/;

function accentFillTextOffenders(css, file) {
  const out = [];
  const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({
    selector: m[1].trim().split("\n").pop().trim(),
    body: m[2],
  }));
  const colorOf = (body) => body.match(/(?:^|[;\s])color\s*:\s*([^;]+)/)?.[1].trim();
  const baseColor = new Map();
  for (const r of rules) {
    const c = colorOf(r.body);
    if (c) for (const part of r.selector.split(",")) baseColor.set(part.trim(), c);
  }
  for (const r of rules) {
    if (!/(?:^|[;\s])background(?:-color)?\s*:\s*var\(--rb-accent\b(?!-)/.test(r.body)) continue;
    let color = colorOf(r.body);
    if (!color) {
      for (const part of r.selector.split(",")) {
        const base = part.trim().replace(STATE_PSEUDO, "");
        if (base !== part.trim() && baseColor.has(base)) color = baseColor.get(base);
      }
    }
    if (color && !/^var\(--rb-on-accent\b/.test(color)) out.push(`${file} ${r.selector}`);
    // A descendant rule (`.x:hover .artist { color: #fff }`) repaints text
    // inside the fill and wins over the inherited on-accent color (Codex on
    // #5727, AddTrackSearch's hovered artist), so it is held to the same rule.
    // A descendant declared on the BASE selector (`.x .artist`, no :hover)
    // also paints inside the hovered fill unless a state rule for the same
    // descendant (`.x:hover .artist`) overrides it with --rb-on-accent.
    // Matching is textual: a descendant styled through a different ancestor
    // (`.list .artist` for an .artist inside `.list button:hover`) is not
    // seen here; resolving DOM nesting is the rendered audit's job.
    const onAccent = (c) => /^var\(--rb-on-accent\b/.test(c);
    const stateColor = new Map();
    for (const d of rules) {
      const dc = colorOf(d.body);
      if (dc) for (const q of d.selector.split(",")) stateColor.set(q.trim(), dc);
    }
    for (const part of r.selector.split(",").map((p) => p.trim())) {
      const base = part.replace(STATE_PSEUDO, "");
      for (const d of rules) {
        const dc = colorOf(d.body);
        if (!dc || onAccent(dc)) continue;
        for (const q of d.selector.split(",").map((x) => x.trim())) {
          if (q.startsWith(`${part} `)) out.push(`${file} ${d.selector}`);
          else if (base !== part && q.startsWith(`${base} `)) {
            const override = stateColor.get(`${part} ${q.slice(base.length + 1)}`);
            if (!override || !onAccent(override)) out.push(`${file} ${d.selector}`);
          }
        }
      }
    }
  }
  return [...new Set(out)];
}

test("PAIR-08 every rule with text on an --rb-accent fill uses --rb-on-accent", () => {
  const walk = (dir) =>
    readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
      e.isDirectory() ? walk(join(dir, e.name)) : [join(dir, e.name)],
    );
  const files = walk(SRC_ROOT).filter((f) => /\.(svelte|css)$/.test(f));
  // Presence control: the scan must see the rules it guards.
  const seen = [];
  const offenders = [];
  for (const f of files) {
    const rel = relative(SRC_ROOT, f).split(sep).join("/");
    const css = readFileSync(f, "utf8");
    if (/background:\s*var\(--rb-accent/.test(css)) seen.push(rel);
    for (const hit of accentFillTextOffenders(css, rel)) if (!ACCENT_TEXT_ALLOWLIST.has(hit)) offenders.push(hit);
  }
  assert.ok(seen.includes("lib/components/rb/CreatePairingSheet.svelte"), `scan missed CreatePairingSheet (${seen.length} files seen)`);
  assert.deepEqual(offenders, [], `text on an accent fill not taken from --rb-on-accent:\n${offenders.join("\n")}`);
});

test("PAIR-08 mutation: the pre-fix Capture rule is an offender, the fixed one is not", () => {
  const before = ".primary {\n\tbackground: var(--rb-accent, #3d7dd9);\n\tcolor: #fff;\n}";
  const after = ".primary {\n\tbackground: var(--rb-accent, #3d7dd9);\n\tcolor: var(--rb-on-accent, #fff);\n}";
  const fill = ".bar {\n\tbackground: var(--rb-accent);\n}";
  assert.deepEqual(accentFillTextOffenders(before, "x"), ["x .primary"]);
  assert.deepEqual(accentFillTextOffenders(after, "x"), []);
  assert.deepEqual(accentFillTextOffenders(fill, "x"), [], "a text-free fill is not an offender");
  // The colour comes from the base rule (StripWaveform's .play-hint shape).
  const hoverOnly = ".hint {\n\tcolor: #fff;\n}\n.hint:hover {\n\tbackground: var(--rb-accent);\n}";
  const hoverFixed = ".hint {\n\tcolor: #fff;\n}\n.hint:hover {\n\tbackground: var(--rb-accent);\n\tcolor: var(--rb-on-accent);\n}";
  assert.deepEqual(accentFillTextOffenders(hoverOnly, "x"), ["x .hint:hover"]);
  assert.deepEqual(accentFillTextOffenders(hoverFixed, "x"), []);
  // A descendant of the fill that sets its own color (AddTrackSearch's shape).
  const desc = ".r:hover {\n\tbackground: var(--rb-accent);\n\tcolor: var(--rb-on-accent);\n}\n.r:hover .artist {\n\tcolor: #fff;\n}";
  const descFixed = desc.replace("color: #fff", "color: var(--rb-on-accent, #fff)");
  assert.deepEqual(accentFillTextOffenders(desc, "x"), ["x .r:hover .artist"]);
  assert.deepEqual(accentFillTextOffenders(descFixed, "x"), []);
  // A base-selector descendant still paints inside the hovered fill (Codex on
  // #5727), unless a state rule for the same descendant overrides it.
  const baseDesc = ".r:hover {\n\tbackground: var(--rb-accent);\n}\n.r .artist {\n\tcolor: #fff;\n}";
  const baseOverridden = `${baseDesc}\n.r:hover .artist {\n\tcolor: var(--rb-on-accent);\n}`;
  const unrelated = ".r:hover {\n\tbackground: var(--rb-accent);\n}\n.other .artist {\n\tcolor: #fff;\n}";
  assert.deepEqual(accentFillTextOffenders(baseDesc, "x"), ["x .r .artist"]);
  assert.deepEqual(accentFillTextOffenders(baseOverridden, "x"), []);
  assert.deepEqual(accentFillTextOffenders(unrelated, "x"), [], "text outside the fill is not an offender");
});

test("PAIRINGS covers every colour token theme.css declares, except a named decorative exemption", () => {
  const tokens = cc.parseColorTokens(THEME_CSS, ".perf-root");
  const covered = new Set();
  for (const p of cc.PAIRINGS) {
    covered.add(p.fg);
    covered.add(p.bg);
  }
  const exempt = new Set(cc.DECORATIVE_TOKENS);
  const uncovered = Object.keys(tokens).filter(
    (name) => !covered.has(name) && !exempt.has(name),
  );
  assert.deepEqual(uncovered, [], `token(s) with no contrast pairing at all: ${uncovered}`);
});

// ----- issue #4219: waveform band palettes, both choices, both themes --------
// The default is rekordbox 3Band; the legacy palette is a CSS override block
// layered over each scheme. Every band must clear the pinned non-text floor
// (3:1) against BOTH row backgrounds in all four combinations.
const WAVE_BAND_TOKENS = ["rb-wave-low", "rb-wave-mid", "rb-wave-high", "rb-wave-mono"];
const SCHEME_SELECTORS = {
  dark: { base: ".perf-root", legacy: "html[data-wave-palette='legacy'] .perf-root" },
  light: {
    base: "html[data-theme='light'] .perf-root",
    legacy: "html[data-theme='light'][data-wave-palette='legacy'] .perf-root",
  },
};

for (const [scheme, sel] of Object.entries(SCHEME_SELECTORS)) {
  for (const choice of ["rekordbox", "legacy"]) {
    test(`#4219 every waveform band clears 3:1 on both rows: ${scheme} / ${choice}`, () => {
      const base = cc.parseColorTokens(THEME_CSS, sel.base);
      const tokens =
        choice === "legacy" ? { ...base, ...cc.parseColorTokens(THEME_CSS, sel.legacy) } : base;
      // Presence first: a pairing set that silently lost its band rows would
      // also report zero violations.
      for (const band of WAVE_BAND_TOKENS) {
        for (const bg of ["rb-bg", "rb-waverow-secondary"]) {
          assert.ok(
            cc.PAIRINGS.some((p) => p.fg === band && p.bg === bg && p.level === "non-text"),
            `no non-text pairing pins ${band} on ${bg}`,
          );
          assert.match(tokens[band] ?? "", /^#[0-9a-f]{6}$/i, `${scheme}/${choice} lacks ${band}`);
          const ratio = cc.contrastRatio(tokens[band], tokens[bg]);
          assert.ok(
            ratio >= cc.AA_LARGE_TEXT_OR_NON_TEXT_UI,
            `${scheme}/${choice} ${band} ${tokens[band]} on ${bg} = ${ratio.toFixed(2)}:1`,
          );
        }
      }
      const violations = cc.validateScheme(tokens, cc.PAIRINGS);
      assert.deepEqual(violations, [], cc.describeViolations(violations));
    });
  }
}

test("#4219 mutation: the dark face's white high band is rejected on the light face", () => {
  const light = cc.parseColorTokens(THEME_CSS, SCHEME_SELECTORS.light.base);
  const dark = cc.parseColorTokens(THEME_CSS, SCHEME_SELECTORS.dark.base);
  const broken = { ...light, "rb-wave-high": dark["rb-wave-high"] };
  const violations = cc.validateScheme(broken, cc.PAIRINGS);
  assert.ok(
    violations.some((v) => v.pairing.fg === "rb-wave-high"),
    "a near-white high band on the light face must violate its pinned pairing",
  );
});
