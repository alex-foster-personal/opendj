/**
 * The palette's TS mirror, checked at its own definition site (pin
 * 5503680a4e0f, CI ratchet follow-up).
 *
 * theme.css is still the ONE place the actual colours are declared - this
 * module does not replace that, and does not read the CSS file at runtime
 * (a `?raw` CSS import would need a bundler plugin `load-typescript.mjs`
 * does not carry, breaking every existing esbuild-based test that already
 * loads `prefs.svelte.ts`). Instead it holds a literal copy of the two
 * shipped schemes' hex values, immediately validated against
 * `color-contrast.ts`'s `PAIRINGS` right here, and a drift test
 * (theme-tokens.test.mjs) asserts these literals equal
 * `parseColorTokens(theme.css, ...)` - so a hand-edit here or in theme.css
 * that goes out of sync fails a test rather than silently drifting.
 *
 * `prefs.svelte.ts` (a real production module - it runs on every boot and
 * every theme toggle) calls `validateActiveScheme` from `_applyThemeDom`,
 * which is what makes this validator load-bearing rather than an orphan
 * only its own test imports.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 DARK_SCHEME_TOKENS/LIGHT_SCHEME_TOKENS: literal mirrors of
 *     theme.css, drift-checked against parseColorTokens.
 *     [if] a palette edit in theme.css is not mirrored here [then ⛔️] the
 *       drift test catches it, not silence
 *   ✔︎ 🎯 validateActiveScheme: runs validateScheme against the active
 *     theme's tokens and LOGS (does not throw - a bad scheme should not
 *     crash the app) any violation, with an injectable logger for tests.
 *     [if] a genuinely broken scheme logs nothing [then ⛔️] broken
 */

import {
  describeViolations,
  PAIRINGS,
  validateScheme,
  type ContrastViolation,
} from './color-contrast';

export type SchemeName = 'dark' | 'light';

/** Mirrors `.perf-root` in theme.css. Keep in sync by hand; the drift test
 * (theme-tokens.test.mjs) fails loudly if this and theme.css disagree. */
export const DARK_SCHEME_TOKENS: Record<string, string> = {
  'rb-bg': '#0d0f12',
  'rb-panel': '#14171d',
  'rb-panel-raised': '#1a1e25',
  'rb-waverow-secondary': '#1a1f28',
  'rb-border': '#23282f',
  'rb-text': '#c8cdd2',
  'rb-text-dim': '#838990',
  'rb-accent': '#2f6fd6',
  'rb-green': '#35c04f',
  'rb-orange': '#e8a13a',
  'rb-wave-low': '#2767d8',
  'rb-wave-mid': '#f0a020',
  'rb-wave-high': '#f4f6f8',
  'rb-wave-mono': '#3d7dd9',
  'rb-red': '#d0342c',
  'rb-yellow': '#e5c33a',
  'rb-select': '#1d3f73',
  'rb-master': '#c9b35a',
  'rb-master-ink': '#1a1608',
  'rb-master-text': '#e8d78a',
};

/** Mirrors `html[data-theme='light'] .perf-root` in theme.css. */
export const LIGHT_SCHEME_TOKENS: Record<string, string> = {
  'rb-bg': '#f7f3eb',
  'rb-panel': '#fffdf8',
  'rb-panel-raised': '#eee8dd',
  'rb-waverow-secondary': '#fffdf8',
  'rb-border': '#b8b0a3',
  'rb-text': '#27241f',
  'rb-text-dim': '#5d574e',
  'rb-accent': '#175ea8',
  'rb-green': '#1d7035',
  'rb-orange': '#a44b11',
  'rb-wave-low': '#1d4fa3',
  'rb-wave-mid': '#9a5a00',
  'rb-wave-high': '#4a4f57',
  'rb-wave-mono': '#2166b1',
  'rb-red': '#ad2420',
  'rb-yellow': '#927000',
  'rb-select': '#d6e5f5',
  'rb-master': '#856500',
  'rb-master-ink': '#fffdf8',
  'rb-master-text': '#4a3700',
};

/** Mirrors the two `[data-wave-palette='legacy']` override blocks in
 * theme.css (issue #4219): the pre-#4219 waveform bands, layered over the
 * scheme's own tokens when the user picks the legacy waveform palette. */
export const LEGACY_WAVE_TOKENS: Record<SchemeName, Record<string, string>> = {
  dark: { 'rb-wave-low': '#e8a13a', 'rb-wave-mid': '#3d7dd9', 'rb-wave-high': '#cfe0f2' },
  light: { 'rb-wave-low': '#a44b11', 'rb-wave-mid': '#2166b1', 'rb-wave-high': '#3a4653' },
};

export function getSchemeTokens(
  theme: SchemeName,
  wavePalette: 'rekordbox' | 'legacy' = 'rekordbox',
): Record<string, string> {
  const base = theme === 'dark' ? DARK_SCHEME_TOKENS : LIGHT_SCHEME_TOKENS;
  return wavePalette === 'legacy' ? { ...base, ...LEGACY_WAVE_TOKENS[theme] } : base;
}

/**
 * Validate the active scheme's tokens and log (never throw) any violation.
 * `tokens` defaults to the real shipped scheme; tests override it to prove
 * the wiring actually fires against a deliberately broken fixture.
 */
export function validateActiveScheme(
  theme: SchemeName,
  log: (message: string) => void = (message) => console.error(message),
  tokens: Record<string, string> = getSchemeTokens(theme),
): ContrastViolation[] {
  const violations = validateScheme(tokens, PAIRINGS);
  if (violations.length > 0) {
    log(
      `[theme] "${theme}" colour scheme fails its own contrast rules:\n${describeViolations(violations)}`,
    );
  }
  return violations;
}
