/**
 * Pin 5503680a4e0f follow-up (CI ratchet, batch9c review): color-contrast.ts
 * must be load-bearing in production, not just imported by its own test.
 * theme-tokens.ts is the palette's TS mirror - the one thing that actually
 * calls validateScheme at the scheme's own definition site, AND the thing
 * prefs.svelte.ts (a real production module, applied on every boot and every
 * theme toggle) imports.
 *
 * Regression lines:
 * - if DARK_SCHEME_TOKENS/LIGHT_SCHEME_TOKENS drift from theme.css's actual
 *   declarations then the validator is checking numbers nobody ships
 * - if validateActiveScheme stays silent on a genuinely broken scheme then
 *   the whole point of running this at apply-time is lost
 * - if the real shipped schemes ever log a violation then the palette (not
 *   this test) is broken
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { before, test } from "node:test";
import { fileURLToPath } from "node:url";

import { loadTypeScriptModule } from "./load-typescript.mjs";

let tt;
let cc;
before(async () => {
  tt = await loadTypeScriptModule("src/lib/rb/theme-tokens.ts");
  cc = await loadTypeScriptModule("src/lib/rb/color-contrast.ts");
});

const THEME_CSS = readFileSync(
  fileURLToPath(new URL("../../src/lib/rb/theme.css", import.meta.url)),
  "utf8",
);

test("DARK_SCHEME_TOKENS mirrors theme.css's actual .perf-root block exactly", () => {
  const parsed = cc.parseColorTokens(THEME_CSS, ".perf-root");
  assert.deepEqual(tt.DARK_SCHEME_TOKENS, parsed);
});

test("LIGHT_SCHEME_TOKENS mirrors theme.css's actual light block exactly", () => {
  const parsed = cc.parseColorTokens(THEME_CSS, "html[data-theme='light'] .perf-root");
  assert.deepEqual(tt.LIGHT_SCHEME_TOKENS, parsed);
});

test("#4219 LEGACY_WAVE_TOKENS mirrors theme.css's legacy waveform override blocks", () => {
  assert.deepEqual(
    tt.LEGACY_WAVE_TOKENS.dark,
    cc.parseColorTokens(THEME_CSS, "html[data-wave-palette='legacy'] .perf-root"),
  );
  assert.deepEqual(
    tt.LEGACY_WAVE_TOKENS.light,
    cc.parseColorTokens(THEME_CSS, "html[data-theme='light'][data-wave-palette='legacy'] .perf-root"),
  );
});

test("#4219 getSchemeTokens layers the legacy waveform bands, and the result still validates", () => {
  assert.equal(tt.getSchemeTokens("dark")["rb-wave-low"], "#2767d8");
  assert.equal(tt.getSchemeTokens("dark", "legacy")["rb-wave-low"], "#e8a13a");
  for (const theme of ["dark", "light"]) {
    for (const choice of ["rekordbox", "legacy"]) {
      const violations = cc.validateScheme(tt.getSchemeTokens(theme, choice), cc.PAIRINGS);
      assert.deepEqual(violations, [], `${theme}/${choice}: ${cc.describeViolations(violations)}`);
    }
  }
});

test("getSchemeTokens returns the matching mirror for each theme", () => {
  assert.deepEqual(tt.getSchemeTokens("dark"), tt.DARK_SCHEME_TOKENS);
  assert.deepEqual(tt.getSchemeTokens("light"), tt.LIGHT_SCHEME_TOKENS);
});

test("validateActiveScheme logs nothing for the real, passing dark scheme", () => {
  const logged = [];
  const violations = tt.validateActiveScheme("dark", (m) => logged.push(m));
  assert.deepEqual(violations, []);
  assert.deepEqual(logged, []);
});

test("validateActiveScheme logs nothing for the real, passing light scheme", () => {
  const logged = [];
  const violations = tt.validateActiveScheme("light", (m) => logged.push(m));
  assert.deepEqual(violations, []);
  assert.deepEqual(logged, []);
});

test("validateActiveScheme LOGS a violation for a deliberately broken scheme (mutation check)", () => {
  const logged = [];
  // Prove the wiring actually fires: patch a token to something unreadable
  // (equal to its own background) and confirm the logged report carries the
  // pairing's own label and both numbers.
  const broken = { ...tt.DARK_SCHEME_TOKENS, "rb-text-dim": "#1a1e25" };
  const violations = tt.validateActiveScheme("dark", (m) => logged.push(m), broken);
  assert.ok(violations.length > 0, "the broken fixture must actually violate something");
  assert.equal(logged.length, 1, "a violation must be logged exactly once");
  assert.match(logged[0], /dim text/);
  assert.match(logged[0], /4\.5/);
});
