/**
 * Per-skin colour tokens for the WCAG validator (issue #13873).
 *
 * theme.css layers each skin's `html[data-skin='<id>'] .perf-root` block over
 * a base scheme (dark `.perf-root`, or the light block when the skin's
 * `html[data-skin='<id>']` block sets `color-scheme: light`). This merges the
 * two so color-contrast.ts's PAIRINGS can be run against what a skin paints.
 *
 * Requirements (mini-PRD):
 *   ? skinTokens: base scheme tokens, then the skin block's tokens on top.
 *     [if] a skin block sets --rb-text equal to --rb-bg [then ⛔️] validateScheme reports it
 *   ? skinTokens: a non-default skin with no parsable block returns null.
 *     [if] a missing block silently validates the base scheme [then ⛔️] broken
 */

import { parseColorTokens } from './color-contrast';

export const DARK_BASE_SELECTOR = '.perf-root';
export const LIGHT_BASE_SELECTOR = "html[data-theme='light'] .perf-root";

export function skinBlockSelector(skin: string): string {
	return `html[data-skin='${skin}'] .perf-root`;
}

/** 'light' when the skin's root block sets `color-scheme: light`, else 'dark'. */
export function skinBaseScheme(css: string, skin: string): 'dark' | 'light' {
	const marker = `html[data-skin='${skin}'] {`;
	const start = css.indexOf(marker);
	if (start === -1) return 'dark';
	const end = css.indexOf('}', start);
	const block = css.slice(start + marker.length, end === -1 ? undefined : end);
	return /color-scheme:\s*light\b/.test(block) ? 'light' : 'dark';
}

/**
 * Merged tokens for one skin. `default` is the dark base alone. Returns null
 * when a non-default skin has no block or its block declares no hex tokens.
 */
export function skinTokens(css: string, skin: string): Record<string, string> | null {
	const dark = parseColorTokens(css, DARK_BASE_SELECTOR);
	if (skin === 'default') return dark;
	let own: Record<string, string>;
	try {
		own = parseColorTokens(css, skinBlockSelector(skin));
	} catch {
		return null;
	}
	if (Object.keys(own).length === 0) return null;
	const base =
		skinBaseScheme(css, skin) === 'light'
			? { ...dark, ...parseColorTokens(css, LIGHT_BASE_SELECTOR) }
			: dark;
	return { ...base, ...own };
}
