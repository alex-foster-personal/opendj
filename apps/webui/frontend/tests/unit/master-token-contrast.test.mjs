/**
 * The master-deck gold is a theme token (--rb-master, --rb-master-ink,
 * --rb-master-text in theme.css), not a hard-coded #c9b35a.
 *
 * Why: the hard-coded gold and its pale row text were tuned for the dark
 * palette only. On the light palette the master row title measured 1.18:1
 * against its own gold wash, i.e. unreadable.
 *
 * Regression lines:
 * - if either theme's master tokens miss their WCAG floor (3:1 indicator,
 *   4.5:1 text) then the master row / MASTER button is unreadable there
 * - if the master row's title text fails 4.5:1 on the ACTUAL composited
 *   row background (28% gold wash, 36% on hover over raised chrome) then the
 *   flat token pairing in color-contrast.ts is passing for the wrong surface
 * - if a component reintroduces the hard-coded gold then the light theme
 *   regresses silently, because no token pairing measures a literal
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

let cc;
before(async () => {
	cc = await loadTypeScriptModule('src/lib/rb/color-contrast.ts');
});

const read = (rel) => readFileSync(fileURLToPath(new URL(`../../src/${rel}`, import.meta.url)), 'utf8');
const THEME_CSS = read('lib/rb/theme.css');
const SELECTORS = {
	dark: '.perf-root',
	light: "html[data-theme='light'] .perf-root"
};

/** color-mix(in srgb, fg p, bg): per-channel interpolation of the encoded
 * sRGB values, which is also what a p-alpha wash over bg composites to. */
function mix(fgHex, p, bgHex) {
	const ch = (hex, i) => parseInt(hex.slice(1 + 2 * i, 3 + 2 * i), 16);
	return (
		'#' +
		[0, 1, 2]
			.map((i) => Math.round(ch(fgHex, i) * p + ch(bgHex, i) * (1 - p)))
			.map((v) => v.toString(16).padStart(2, '0'))
			.join('')
	);
}

for (const [theme, selector] of Object.entries(SELECTORS)) {
	test(`${theme}: master tokens are declared and meet their WCAG floors`, () => {
		const tokens = cc.parseColorTokens(THEME_CSS, selector);
		for (const name of ['rb-master', 'rb-master-ink', 'rb-master-text']) {
			assert.match(tokens[name] ?? '', /^#[0-9a-f]{6}$/i, `${theme} declares --${name}`);
		}
		const masterPairings = cc.PAIRINGS.filter((p) => p.fg.startsWith('rb-master') || p.bg.startsWith('rb-master'));
		assert.equal(masterPairings.length, 3, 'indicator, ink-on-fill and row-text pairings are declared');
		const violations = cc.validateScheme(tokens, masterPairings);
		assert.deepEqual(violations, [], cc.describeViolations(violations));
		// Presence, not just absence: measure the numbers themselves.
		assert.ok(cc.contrastRatio(tokens['rb-master'], tokens['rb-bg']) >= 3);
		assert.ok(cc.contrastRatio(tokens['rb-master-ink'], tokens['rb-master']) >= 4.5);
	});

	test(`${theme}: master row title text is AA on the composited gold wash`, () => {
		const t = cc.parseColorTokens(THEME_CSS, selector);
		const surfaces = {
			'row on panel (28% wash)': mix(t['rb-master'], 0.28, t['rb-panel']),
			'row on window bg (28% wash)': mix(t['rb-master'], 0.28, t['rb-bg']),
			'hovered row (36% over raised chrome)': mix(t['rb-master'], 0.36, t['rb-panel-raised'])
		};
		for (const [label, bg] of Object.entries(surfaces)) {
			const ratio = cc.contrastRatio(t['rb-master-text'], bg);
			assert.ok(ratio >= 4.5, `${theme} ${label}: ${ratio.toFixed(2)}:1 < 4.5:1`);
		}
	});
}

test('control: the old hard-coded gold FAILS on the light palette (the test can say no)', () => {
	const light = cc.parseColorTokens(THEME_CSS, SELECTORS.light);
	assert.ok(cc.contrastRatio('#c9b35a', light['rb-bg']) < 3);
	assert.ok(cc.contrastRatio('#e8d78a', mix('#c9b35a', 0.28, light['rb-panel'])) < 4.5);
});

test('master chrome reads the token, never a hard-coded gold', () => {
	const files = {
		'lib/rb/theme.css': THEME_CSS,
		'lib/components/rb/browser/TrackTable.svelte': read('lib/components/rb/browser/TrackTable.svelte'),
		'lib/components/rb/deck/DeckHeader.svelte': read('lib/components/rb/deck/DeckHeader.svelte')
	};
	for (const [rel, src] of Object.entries(files)) {
		assert.ok(src.includes('var(--rb-master)'), `${rel} uses var(--rb-master)`);
		// The literal may appear only as the token's own dark declaration.
		const literalUses = src.split('\n').filter((line) => /#c9b35a|#e8d78a|201,\s*179,\s*90/i.test(line));
		const allowed = literalUses.filter((line) => /^\s*--rb-master(-text)?:/.test(line));
		assert.deepEqual(literalUses, allowed, `${rel} has a hard-coded master gold outside the token`);
	}
	const table = files['lib/components/rb/browser/TrackTable.svelte'];
	assert.match(table, /tr\.rb-row-master \.c-artist \{\s*color: var\(--rb-master-text\);/);
});
