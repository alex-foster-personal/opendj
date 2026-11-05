/**
 * UX-MENU-01: the top bar's hover menus (AutoPlay, LYR/Stage, mode picker)
 * paint their card with the skin's surface token, never a literal.
 *
 * Why: `.ap-menu` hardcoded `background: #0a0c0f` while its text used
 * `--rb-text` / `--rb-text-dim`. The Light skin swaps those to dark ink, so
 * the rendered menu read 1.27:1 (headings, labels) and 2.74:1 (status lines)
 * against 4.5:1 (W34, measured in headless Chromium, orders board #5638).
 *
 * Regression lines:
 * - a menu card goes back to a literal -> the literal check fails
 * - the token chosen is one whose text pairing misses AA in any skin -> the
 *   per-skin sweep fails, naming the skin and the pair
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { WCAG_AA_BODY_TEXT, contrastRatio, readCssVarHex } from './contrast.mjs';

const read = (rel) =>
	readFileSync(fileURLToPath(new URL(`../../src/${rel}`, import.meta.url)), 'utf8');
const TOPBAR = read('lib/components/rb/TopBar.svelte');
const THEME_CSS = read('lib/rb/theme.css');

const MENUS = ['.ap-menu', '.mode-menu'];
const SKINS = {
	default: '.perf-root {',
	'light theme': "html[data-theme='light'] .perf-root {",
	'light skin': "html[data-skin='light'] .perf-root {",
	gothic: "html[data-skin='mono-dev'] .perf-root {"
};

function ruleBody(source, selectorOpen) {
	const start = source.indexOf(`\n\t${selectorOpen}`);
	assert.notEqual(start, -1, `${selectorOpen} not found in TopBar.svelte`);
	return source.slice(start, source.indexOf('}', start));
}

function skinBlock(selectorOpen) {
	const start = THEME_CSS.split('\n').findIndex((line) => line.startsWith(selectorOpen));
	assert.notEqual(start, -1, `${selectorOpen} not found in theme.css`);
	const lines = THEME_CSS.split('\n').slice(start);
	return lines.slice(0, lines.findIndex((line) => line.startsWith('}')) + 1).join('\n');
}

function menuBackgroundToken(menu) {
	const m = /\n\s*background:\s*([^;]+);/.exec(ruleBody(TOPBAR, `${menu} {`));
	assert.ok(m, `${menu} sets no background`);
	return m[1].trim();
}

for (const menu of MENUS) {
	test(`${menu} paints its card with a theme token, not a literal`, () => {
		const bg = menuBackgroundToken(menu);
		assert.match(bg, /^var\(--rb-[a-z-]+\)$/, `${menu} background is "${bg}"`);
	});

	test(`${menu} text meets AA on its card in every skin`, () => {
		const token = /^var\(--(rb-[a-z-]+)\)$/.exec(menuBackgroundToken(menu))[1];
		for (const [skin, selector] of Object.entries(SKINS)) {
			const block = skinBlock(selector);
			const card = readCssVarHex(block, token);
			for (const fg of ['rb-text', 'rb-text-dim']) {
				const ratio = contrastRatio(readCssVarHex(block, fg), card);
				assert.ok(
					ratio >= WCAG_AA_BODY_TEXT,
					`${skin}: --${fg} on --${token} is ${ratio.toFixed(2)}:1 in ${menu}`
				);
			}
		}
	});
}

test('negative control: the old literal card fails the Light sweep', () => {
	const light = skinBlock(SKINS['light skin']);
	const oldCard = { r: 0x0a, g: 0x0c, b: 0x0f };
	assert.ok(contrastRatio(readCssVarHex(light, 'rb-text'), oldCard) < WCAG_AA_BODY_TEXT);
});
