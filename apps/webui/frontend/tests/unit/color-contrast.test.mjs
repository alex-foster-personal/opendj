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
import { readFileSync } from "node:fs";
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
