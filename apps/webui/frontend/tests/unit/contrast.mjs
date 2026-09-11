/**
 * WCAG 2.1 contrast arithmetic, plus the composited backdrops the BPM column's
 * readability floor is measured against.
 *
 * TEST SUPPORT, not shipped code, and that is the point rather than an
 * accident. Colour floors are decided when a palette is written: the clamps
 * they produce are literals in $lib/rb/bpm-heat, so the browser carries an
 * answer instead of a search loop plus the whole of WCAG's arithmetic on a
 * /performance surface that sits at its gzip ratchet. Keeping the arithmetic
 * here rather than in src is what makes that true by construction - there is
 * nothing for a bundler to include, and nothing in src exported for a reader
 * that never comes.
 *
 * Shared on purpose (groundwork for issue #888, a cross-project
 * minimum-contrast requirement): any unit test can enumerate its own states
 * against this instead of reimplementing the arithmetic.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/** WCAG AA for body text. Small UI numerals are body text; the 3:1 large-text
 * allowance does not apply to them. */
export const WCAG_AA_BODY_TEXT = 4.5;

function _channelToLinear(value) {
	const s = value / 255;
	return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

export function relativeLuminance(c) {
	return (
		0.2126 * _channelToLinear(c.r) +
		0.7152 * _channelToLinear(c.g) +
		0.0722 * _channelToLinear(c.b)
	);
}

/** Contrast ratio, 1..21. Symmetric in its arguments. */
export function contrastRatio(a, b) {
	const la = relativeLuminance(a);
	const lb = relativeLuminance(b);
	return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

function _hexToRgb(hex) {
	const m = /^#([0-9a-fA-F]{6})$/.exec(hex.trim());
	if (m === null) throw new Error(`not a 6-digit hex color literal: "${hex}"`);
	const n = parseInt(m[1], 16);
	return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

/** Reads a `--name: #hex;` custom property out of a CSS source string. Throws
 * rather than returning undefined: per .claude/rules/verification.md, a
 * check that finds nothing must not render as a result, and a silently
 * absent variable would composite to NaN and pass the sweep below against a
 * fiction, which is the exact defect this fixture replaces. */
export function readCssVarHex(cssSource, varName) {
	const m = new RegExp(`--${varName}:\\s*(#[0-9a-fA-F]{6})`).exec(cssSource);
	if (m === null) {
		throw new Error(`--${varName} not found in the given CSS - contrast fixture is stale`);
	}
	return _hexToRgb(m[1]);
}

/**
 * Composites a `.c-bpm.bpm-<lane> { ... background: color-mix(in srgb, <tint>
 * <pct>%, transparent); ... }` rule from the given source over `base`, the
 * way the browser renders that color-mix() over the row's own background.
 * `<tint>` may be a literal `#rrggbb` or a `var(--name)` resolved against
 * `themeSource`. Throws if the selector or its color-mix is not found, for
 * the same reason `readCssVarHex` throws.
 */
export function parseColorMixBackdrop(source, themeSource, selector, base) {
	const re = new RegExp(
		`\\.c-bpm\\.bpm-${selector}\\s*\\{[^}]*color-mix\\(in srgb,\\s*([^,]+?)\\s+(\\d+(?:\\.\\d+)?)%,\\s*transparent\\)`
	);
	const m = re.exec(source);
	if (m === null) {
		throw new Error(
			`.c-bpm.bpm-${selector}'s color-mix() was not found - contrast fixture is stale`
		);
	}
	const [, tintExpr, pctStr] = m;
	const pct = Number(pctStr);
	const varMatch = /^var\(--([\w-]+)\)$/.exec(tintExpr.trim());
	const tint = varMatch === null ? _hexToRgb(tintExpr.trim()) : readCssVarHex(themeSource, varMatch[1]);
	const mix = (a, b) => Math.round(a * (1 - pct / 100) + b * (pct / 100));
	return { r: mix(base.r, tint.r), g: mix(base.g, tint.g), b: mix(base.b, tint.b) };
}

function _read(relFromHere) {
	return readFileSync(fileURLToPath(new URL(relFromHere, import.meta.url)), 'utf8');
}

const THEME_CSS = _read('../../src/lib/rb/theme.css');
const TRACK_TABLE_SVELTE = _read('../../src/lib/components/rb/browser/TrackTable.svelte');

const RB_PANEL = readCssVarHex(THEME_CSS, 'rb-panel');

/**
 * What each BPM lane's text sits on, with the lane tint composited over the
 * default browser row surface (`--rb-panel`) - derived from the SHIPPED
 * `theme.css` and `TrackTable.svelte` sources above, not hand-flattened, so
 * a change to either file's colours or color-mix() percentages moves this
 * with it instead of leaving it to bind a fiction (pin r3915742296).
 */
export const BPM_LANE_BACKDROP = {
	sweet: parseColorMixBackdrop(TRACK_TABLE_SVELTE, THEME_CSS, 'sweet', RB_PANEL),
	half: parseColorMixBackdrop(TRACK_TABLE_SVELTE, THEME_CSS, 'half', RB_PANEL),
	mid: RB_PANEL,
	far: RB_PANEL
};
