/**
 * WCAG contrast validation for /performance colour schemes (issue behind pin
 * 5503680a4e0f, task 2 remainder).
 *
 * PR #1062 already shipped the separate persisted light/dark toggle and a
 * basic light palette (theme.css). What was still missing is a "usability
 * rule": something that MECHANICALLY proves a colour scheme is readable,
 * rather than a comment asserting it. theme.css's own light-palette comment
 * claimed "every foreground/background token pair is AA at the token
 * level" - this module is what makes that claim checkable, for the two
 * shipped schemes today and for any user-authored scheme later.
 *
 * Dependency-free on purpose, same rationale as feedback.ts: the luminance
 * math, the CSS token parse and the pairing checks are the parts worth
 * unit-testing without a DOM.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 relativeLuminance/contrastRatio: the standard WCAG 2.x formula.
 *     [if] contrastRatio drifts from the published formula [then ⛔️] every
 *       verdict downstream is silently wrong
 *   ✔︎ 🎯 parseColorTokens: reads only the hex custom properties out of ONE
 *     named selector's declaration block, never a later block that reuses
 *     the same property names (theme.css has exactly this shape: `.perf-root`
 *     for dark, `html[data-theme='light'] .perf-root` for light).
 *     [if] the light block's tokens leak into the dark read [then ⛔️] broken
 *   ✔︎ 🎯 validateScheme: every declared pairing's ratio is checked against
 *     its stated threshold (4.5:1 body text, 3:1 large text / non-text UI
 *     indicators); a missing token is a violation, never a silent skip.
 *     [if] a pairing under its threshold is not reported [then ⛔️] broken
 *   ✔︎ 🎯 PAIRINGS + the shipped-palette tests: the repo's ACTUAL dark and
 *     light schemes are run through validateScheme and must report zero
 *     violations. A real violation found here gets the PALETTE fixed, never
 *     the threshold lowered.
 */

// ----- the WCAG math -------------------------------------------------------
export const AA_BODY_TEXT = 4.5;
export const AA_LARGE_TEXT_OR_NON_TEXT_UI = 3.0;

function srgbChannelToLinear(c: number): number {
  const normalized = c / 255;
  return normalized <= 0.04045
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4;
}

function parseHex(hex: string): { r: number; g: number; b: number } {
  const match = /^#([0-9a-fA-F]{6})$/.exec(hex);
  if (!match) throw new Error(`not a 6-digit hex colour: ${hex}`);
  const value = match[1];
  return {
    r: parseInt(value.slice(0, 2), 16),
    g: parseInt(value.slice(2, 4), 16),
    b: parseInt(value.slice(4, 6), 16),
  };
}

/** WCAG relative luminance, 0 (black) to 1 (white). */
export function relativeLuminance(hex: string): number {
  const { r, g, b } = parseHex(hex);
  return (
    0.2126 * srgbChannelToLinear(r) +
    0.7152 * srgbChannelToLinear(g) +
    0.0722 * srgbChannelToLinear(b)
  );
}

/** WCAG contrast ratio, 1 (identical) to 21 (black vs white). Order-independent. */
export function contrastRatio(hexA: string, hexB: string): number {
  const a = relativeLuminance(hexA);
  const b = relativeLuminance(hexB);
  const lighter = Math.max(a, b);
  const darker = Math.min(a, b);
  return (lighter + 0.05) / (darker + 0.05);
}

// ----- pulling a scheme's tokens out of theme.css --------------------------
/**
 * The hex-valued `--rb-*` custom properties declared inside ONE selector's
 * `{ ... }` block. Non-hex declarations (theme.css also carries a couple of
 * `rgba(...)` glow tokens) are skipped rather than mis-parsed - they are
 * never used as a solid fg or bg, so they have no contrast pairing anyway.
 *
 * Deliberately not a general CSS parser: theme.css's own two palette blocks
 * (`.perf-root`, `html[data-theme='light'] .perf-root`) contain no nested
 * braces, so finding the next `}` after the selector is exact, not a
 * heuristic, for the file this actually reads.
 */
export function parseColorTokens(
  css: string,
  selector: string,
): Record<string, string> {
  const marker = `${selector} {`;
  const start = css.indexOf(marker);
  if (start === -1) {
    throw new Error(`selector not found in stylesheet: ${selector}`);
  }
  const bodyStart = start + marker.length;
  const end = css.indexOf("}", bodyStart);
  if (end === -1) {
    throw new Error(`unterminated declaration block for selector: ${selector}`);
  }
  const block = css.slice(bodyStart, end);
  const tokens: Record<string, string> = {};
  const re = /--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(block)) !== null) {
    tokens[match[1]] = match[2];
  }
  return tokens;
}

// ----- pairings + validation ------------------------------------------------
export type ContrastLevel = "body" | "large" | "non-text";

const THRESHOLD_FOR_LEVEL: Record<ContrastLevel, number> = {
  body: AA_BODY_TEXT,
  large: AA_LARGE_TEXT_OR_NON_TEXT_UI,
  "non-text": AA_LARGE_TEXT_OR_NON_TEXT_UI,
};

export interface ContrastPairing {
  /** Token name WITHOUT the leading `--`, e.g. "rb-text". */
  fg: string;
  bg: string;
  level: ContrastLevel;
  /** What this pairing is, for a human reading a violation report. */
  label: string;
}

export interface ContrastViolation {
  pairing: ContrastPairing;
  /** Present only when both tokens existed and were measured. */
  ratio?: number;
  required: number;
  /** Present only when a declared token was missing from the scheme. */
  reason?: string;
}

/**
 * Every declared fg/bg pairing in the repo's colour system, with the
 * threshold its ROLE actually calls for. Roles come straight from theme.css's
 * own inline comments, which is the single centralised source of truth this
 * module checks:
 *
 *  - `--rb-text` / `--rb-text-dim` are documented as body text and are
 *    checked against every surface they render on (window bg, panel, raised
 *    chrome, the selected-row highlight, and the decks 3/4 waveform row) at 4.5:1.
 *  - `--rb-accent`, `--rb-green`, `--rb-orange`, `--rb-red`, `--rb-yellow`,
 *    and the `--rb-wave-*` bands are documented as indicators and
 *    graphical bands (toggles/lit buttons, loaded-track chip, waveform
 *    bands, cue markers, a badge) rather than paragraph text, so they are
 *    held to the 3:1 non-text-UI floor against the window background they
 *    are drawn on. The waveform bands are also pinned against the decks 3/4
 *    row (`--rb-waverow-secondary`), for both the default rekordbox 3Band
 *    and the legacy waveform palette (issue #4219).
 *
 * Known gap (stated rather than hidden): a handful of call sites still reuse
 * an indicator colour as small `color:` text without a declared pairing
 * (e.g. QualityBadge, error labels). Deck-strip hotcue letter chips
 * (`rb-bg` on `rb-green`) are covered below; the remaining usages are a
 * separate per-usage audit, not this validator's job.
 */
/**
 * Tokens with a deliberately DECORATIVE role only. `--rb-border` is
 * documented in theme.css as "panel dividers" - structural chrome with no
 * state of its own, which WCAG's non-text-contrast rule (1.4.11) does not
 * reach: that rule covers a UI component's boundary or state, not a plain
 * divider line. Named explicitly so "this token has no pairing" reads as a
 * decision `PAIRINGS` coverage checks can rely on, not a gap nobody noticed.
 */
export const DECORATIVE_TOKENS: readonly string[] = ["rb-border"];

export const PAIRINGS: ContrastPairing[] = [
  { fg: "rb-text", bg: "rb-bg", level: "body", label: "primary text on window background" },
  { fg: "rb-text", bg: "rb-panel", level: "body", label: "primary text on panel surface" },
  {
    fg: "rb-text",
    bg: "rb-panel-raised",
    level: "body",
    label: "primary text on raised chrome (buttons, chips)",
  },
  {
    fg: "rb-text",
    bg: "rb-select",
    level: "body",
    label: "primary text on a selected browser row",
  },
  { fg: "rb-text-dim", bg: "rb-bg", level: "body", label: "dim text on window background" },
  { fg: "rb-text-dim", bg: "rb-panel", level: "body", label: "dim text on panel surface" },
  {
    fg: "rb-text-dim",
    bg: "rb-panel-raised",
    level: "body",
    label: "dim text on raised chrome (buttons, chips)",
  },
  {
    fg: "rb-text",
    bg: "rb-waverow-secondary",
    level: "body",
    label: "primary text on deck 3/4 waveform row (WaveGutter deck number)",
  },
  {
    fg: "rb-text-dim",
    bg: "rb-waverow-secondary",
    level: "body",
    label: "dim text on deck 3/4 waveform row (title, bars, empty-state copy)",
  },
  {
    fg: "rb-accent",
    bg: "rb-bg",
    level: "non-text",
    label: "accent indicator (toggles, faders, lit buttons)",
  },
  {
    fg: "rb-green",
    bg: "rb-bg",
    level: "non-text",
    label: "loaded-track / loop-out indicator",
  },
  {
    fg: "rb-bg",
    bg: "rb-green",
    level: "body",
    label: "deck-strip hotcue letter chip (text on green fill)",
  },
  { fg: "rb-orange", bg: "rb-bg", level: "non-text", label: "loop cue / warning indicator" },
  // Waveform bands (issue #4219): every band is pinned against BOTH row
  // backgrounds, decks 1/2 (rb-bg) and decks 3/4 (rb-waverow-secondary).
  { fg: "rb-wave-low", bg: "rb-bg", level: "non-text", label: "waveform lows band" },
  { fg: "rb-wave-mid", bg: "rb-bg", level: "non-text", label: "waveform mids band" },
  { fg: "rb-wave-high", bg: "rb-bg", level: "non-text", label: "waveform highs band" },
  { fg: "rb-wave-mono", bg: "rb-bg", level: "non-text", label: "waveform mono/line design" },
  {
    fg: "rb-wave-low",
    bg: "rb-waverow-secondary",
    level: "non-text",
    label: "waveform lows band on deck 3/4 row",
  },
  {
    fg: "rb-wave-mid",
    bg: "rb-waverow-secondary",
    level: "non-text",
    label: "waveform mids band on deck 3/4 row",
  },
  {
    fg: "rb-wave-high",
    bg: "rb-waverow-secondary",
    level: "non-text",
    label: "waveform highs band on deck 3/4 row",
  },
  {
    fg: "rb-wave-mono",
    bg: "rb-waverow-secondary",
    level: "non-text",
    label: "waveform mono/line design on deck 3/4 row",
  },
  { fg: "rb-red", bg: "rb-bg", level: "non-text", label: "cue marker / position tick" },
  { fg: "rb-yellow", bg: "rb-bg", level: "non-text", label: "Free licence badge" },
  {
    fg: "rb-master",
    bg: "rb-bg",
    level: "non-text",
    label: "master gold indicator (MASTER button, master row edge)",
  },
  {
    fg: "rb-master-ink",
    bg: "rb-master",
    level: "body",
    label: "text on master gold fill (MASTER button, master-fold badge)",
  },
  {
    fg: "rb-master-text",
    bg: "rb-panel",
    level: "body",
    label: "master library-row title/artist text",
  },
];

/**
 * Check every pairing against the scheme's tokens. A pairing whose fg or bg
 * token is simply absent from `tokens` is reported too (as `reason`, no
 * `ratio`) - a scheme with a hole in it is exactly the kind of "not obviously
 * broken" state this exists to catch, not something to skip past quietly.
 */
export function validateScheme(
  tokens: Readonly<Record<string, string>>,
  pairings: readonly ContrastPairing[] = PAIRINGS,
): ContrastViolation[] {
  const violations: ContrastViolation[] = [];
  for (const pairing of pairings) {
    const fgHex = tokens[pairing.fg];
    const bgHex = tokens[pairing.bg];
    const required = THRESHOLD_FOR_LEVEL[pairing.level];
    if (fgHex === undefined || bgHex === undefined) {
      const missing = [
        fgHex === undefined ? pairing.fg : null,
        bgHex === undefined ? pairing.bg : null,
      ]
        .filter((name): name is string => name !== null)
        .join(", ");
      violations.push({ pairing, required, reason: `token(s) not declared: ${missing}` });
      continue;
    }
    const ratio = contrastRatio(fgHex, bgHex);
    if (ratio < required) {
      violations.push({ pairing, ratio, required });
    }
  }
  return violations;
}

export function describeViolations(violations: readonly ContrastViolation[]): string {
  if (violations.length === 0) return "no contrast violations";
  return violations
    .map((v) => {
      const detail =
        v.ratio !== undefined
          ? `${v.ratio.toFixed(2)}:1 measured, needs ${v.required}:1`
          : (v.reason ?? "missing token(s)");
      return `${v.pairing.label} (${v.pairing.fg} on ${v.pairing.bg}): ${detail}`;
    })
    .join("\n");
}
