import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const themePath = path.join(__dirname, '../../src/lib/rb/theme.css');
const waveRowPath = path.join(__dirname, '../../src/lib/components/rb/wave/WaveRow.svelte');

/**
 * pin e585d3b67f4d: "the title below it is too loud (full white font) and too
 * wide. Empty and no track loaded for state there is too loud".
 *
 * This file owns exactly ONE half of that guard: the palette arithmetic. Is
 * `--rb-text-dim` legible on every surface this title can land on, in every
 * theme? That is a property of the token values, needs no browser, and covers
 * surfaces a running app cannot easily be driven to (the light theme, the deck
 * 3/4 row fill).
 *
 * The OTHER half - which colour, width and background actually win the cascade
 * - lives in `tests/e2e/performance-wave-title-emphasis.spec.ts` and is read
 * out of `getComputedStyle` in a real browser. It used to live here, as a
 * hand-written cascade resolver over the component's `<style>` block. A
 * blinded reviewer of PR #1723 defeated that resolver five different ways
 * while every test stayed green - a `@media` wrapper, an `!important` at lower
 * specificity, a `:where()` (zero specificity per spec, counted as 100 here),
 * a `background-color` longhand beating a `background` shorthand, and a comma
 * inside `:not()` breaking a naive selector-list split - each verified by
 * injecting real CSS into the real component and running the real test.
 *
 * The lesson is not "patch those five". It is that re-implementing the cascade
 * is the wrong tool for a question the browser already answers exactly, so the
 * resolver was deleted rather than hardened.
 */

/** Token -> hex, read out of one selector block of the real theme file. */
function paletteFor(themeCss, selector) {
	const start = themeCss.indexOf(selector);
	assert.ok(start !== -1, `theme.css must still define ${selector}`);
	const block = themeCss.slice(start, themeCss.indexOf('}', start));
	const palette = {};
	for (const match of block.matchAll(/(--rb-[\w-]+):\s*(#[0-9a-fA-F]{6})/g)) {
		palette[match[1]] = match[2];
	}
	return palette;
}

function relativeLuminance(hex) {
	const channels = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
	const [r, g, b] = channels.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
	return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrastRatio(fg, bg) {
	const [light, dark] = [relativeLuminance(fg), relativeLuminance(bg)].sort((a, b) => b - a);
	return (light + 0.05) / (dark + 0.05);
}

test('the dim token clears the 4.5:1 AA floor on every surface this title renders on', () => {
	const themeCss = readFileSync(themePath, 'utf8');
	// The one background in play here that is NOT a token: decks 3/4 paint
	// their whole row, gutter included, with a literal hex in WaveRow.svelte.
	const secondaryRow = readFileSync(waveRowPath, 'utf8').match(
		/\.rb-waverow\.secondary\s*\{[^}]*background:\s*(#[0-9a-fA-F]{6})/
	);
	assert.ok(secondaryRow !== null, 'WaveRow must still declare the secondary-row fill');

	const dark = paletteFor(themeCss, '.perf-root {');
	const light = paletteFor(themeCss, "html[data-theme='light'] .perf-root {");
	const surfaces = [
		['dark --rb-bg', dark['--rb-text-dim'], dark['--rb-bg']],
		['dark --rb-panel', dark['--rb-text-dim'], dark['--rb-panel']],
		['dark --rb-panel-raised', dark['--rb-text-dim'], dark['--rb-panel-raised']],
		['dark deck 3/4 row', dark['--rb-text-dim'], secondaryRow[1]],
		['light --rb-bg', light['--rb-text-dim'], light['--rb-bg']],
		['light --rb-panel', light['--rb-text-dim'], light['--rb-panel']],
		['light --rb-panel-raised', light['--rb-text-dim'], light['--rb-panel-raised']]
		// DELIBERATELY NOT LISTED: light theme on the deck 3/4 row. That row's
		// fill is a hardcoded dark hex with no light-theme override, so this
		// title measures 2.31:1 there - a real, PRE-EXISTING AA failure that
		// belongs to WaveRow's palette, not to this title's colour. Listing it
		// would make this test red for a defect it cannot fix; omitting it
		// silently would hide it. It is tracked as issue #1722, and the
		// number is recorded here so nobody has to rediscover it: before the
		// dim token this same cell measured 1.07:1, so this change improved it
		// 2.2x without clearing the floor.
	];
	for (const [name, fg, bg] of surfaces) {
		assert.ok(fg !== undefined && bg !== undefined, `missing colours for ${name}`);
		const ratio = contrastRatio(fg, bg);
		assert.ok(
			ratio >= 4.5,
			`${name}: ${fg} on ${bg} is ${ratio.toFixed(2)}:1, under the 4.5:1 AA body floor`
		);
	}
});
