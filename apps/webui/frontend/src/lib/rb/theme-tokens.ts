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
 *   ✔︎ ✅ 🎯 TOPBAR_H_PX / WAVEROW_H_PX_*: literal px mirrors of theme.css and
 *     the /performance page, drift-checked in perf-layout-feasibility.test.mjs.
 *     [if] theme.css changes --rb-topbar-h and this stays 28 [then ⛔️]
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
  'rb-pair': '#a46ef0',
  'rb-bg': '#0d0f12',
  'rb-panel': '#14171d',
  'rb-panel-raised': '#1a1e25',
  'rb-waverow-secondary': '#1a1f28',
  'rb-border': '#23282f',
  'rb-text': '#c8cdd2',
  'rb-text-dim': '#838990',
  'rb-accent': '#2f6fd6',
  'rb-accent-text': '#8cb0e9',
  'rb-focus-ring': '#ffb43a', // A11Y-11403 keyboard focus ring
  'rb-on-accent': '#ffffff',
  'rb-green': '#35c04f',
  'rb-orange': '#e8a13a',
  'rb-warn-text': '#e8a13a',
  'rb-wave-low': '#2767d8',
  'rb-wave-mid': '#f0a020',
  'rb-wave-high': '#f4f6f8',
  'rb-wave-mono': '#3d7dd9',
  'rb-plan-fader': '#ffffff',
  'rb-plan-eq-low': '#ffc04d',
  'rb-plan-eq-mid': '#7ee787',
  'rb-plan-eq-high': '#ff7eb6',
  'rb-plan-filter': '#4dd8ff',
  'rb-plan-out': '#ff3b30',
  'rb-plan-in': '#35c04f',
  'rb-wave-vocal': '#4fb2ff',
  'rb-red': '#d0342c',
  'rb-yellow': '#e5c33a',
  'rb-select': '#1d3f73',
  'rb-section-edge': '#3fb3c8',
  'rb-section-rise': '#d9a441',
  'rb-section-drop': '#ea4a76',
  'rb-section-calm': '#8b82c4',
  'rb-section-body': '#6e8fa8',
  'rb-section-neutral': '#80868d',
  'rb-master': '#c9b35a',
  'rb-master-ink': '#1a1608',
  'rb-master-text': '#e8d78a',
  'rb-rail-spotify': '#35c04f',
  'rb-rail-files': '#3d7dd9',
  'rb-rail-beatport': '#8e5bd6',
  'rb-rail-record': '#d0342c',
  'rb-color-fx-filter-face': '#f0f2f5',
  'rb-color-fx-filter-face-edge': '#8a8f96',
};

/** Mirrors `html[data-theme='light'] .perf-root` in theme.css. */
export const LIGHT_SCHEME_TOKENS: Record<string, string> = {
  'rb-pair': '#7a3fd6',
  'rb-bg': '#f7f3eb',
  'rb-panel': '#fffdf8',
  'rb-panel-raised': '#eee8dd',
  'rb-waverow-secondary': '#fffdf8',
  'rb-border': '#b8b0a3',
  'rb-text': '#27241f',
  'rb-text-dim': '#5d574e',
  'rb-accent': '#175ea8',
  'rb-accent-text': '#0f3d6e',
  'rb-focus-ring': '#a35a00',
  'rb-on-accent': '#ffffff',
  'rb-green': '#1d7035',
  'rb-orange': '#a44b11',
  'rb-warn-text': '#925d11',
  'rb-wave-low': '#1d4fa3',
  'rb-wave-mid': '#9a5a00',
  'rb-wave-high': '#4a4f57',
  'rb-wave-mono': '#2166b1',
  'rb-plan-fader': '#1b1a17',
  'rb-plan-eq-low': '#8a4b00',
  'rb-plan-eq-mid': '#1a6b33',
  'rb-plan-eq-high': '#a81e62',
  'rb-plan-filter': '#0b6a8a',
  'rb-plan-out': '#b3261e',
  'rb-plan-in': '#1d7035',
  'rb-wave-vocal': '#2a76c0',
  'rb-red': '#ad2420',
  'rb-yellow': '#927000',
  'rb-select': '#d6e5f5',
  'rb-section-edge': '#1f7f92',
  'rb-section-rise': '#a4730f',
  'rb-section-drop': '#b8254f',
  'rb-section-calm': '#5d539a',
  'rb-section-body': '#486a85',
  'rb-section-neutral': '#6b6660',
  'rb-master': '#856500',
  'rb-master-ink': '#fffdf8',
  'rb-master-text': '#4a3700',
  // Light-only skin hooks (CHROME-19); Default and Gothic leave them unset.
  'rb-jog-text': '#c8cdd2',
  'rb-jog-text-dim': '#9aa0a6',
  'rb-well': '#fffdf8',
  'rb-strip-pos': '#27241f',
  'rb-text-muted': '#5d574e',
  'rb-knob-ring': '#ffffff',
  'rb-knob-cap': '#e2e2e2',
  'rb-knob-cap-edge': '#8a8a8a',
  'rb-color-fx-filter-face': '#ffffff',
  'rb-color-fx-filter-face-edge': '#6b6660',
  'rb-knob-indicator-white': '#121212',
  'rb-knob-takeover': '#8a5a00',
  'rb-stem-vocal': '#4f6a80',
  'rb-stem-instrumental': '#566b49',
  'rb-stem-drums': '#7a5f45',
  'rb-stem-bass': '#625a7a',
  'rb-stem-other': '#3f6b63',
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

//----- Layout metrics (literal mirror of theme.css and the /performance page; drift-checked in perf-layout-feasibility.test.mjs)

/** `--rb-topbar-h` in theme.css. */
export const TOPBAR_H_PX = 28;
/** Default skin's `--rb-waverow-h`. */
export const WAVEROW_H_PX_DEFAULT = 43;
/** Tallest skin `--rb-waverow-h` (Gothic, Light, Green Room, Practice). */
export const WAVEROW_H_PX_TALLEST = 56;
/** The /performance page's compact wavestack row (PERF-UI-01), applied below COMPACT_WAVESTACK_BELOW_H_PX. */
export const WAVEROW_H_PX_COMPACT = 22;
export const COMPACT_WAVESTACK_BELOW_H_PX = 800;
