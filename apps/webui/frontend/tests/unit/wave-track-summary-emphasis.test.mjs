import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import { loadTypeScriptModule } from './load-typescript.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const themePath = path.join(__dirname, '../../src/lib/rb/theme.css');
const waveRowPath = path.join(__dirname, '../../src/lib/components/rb/wave/WaveRow.svelte');
const renderPath = path.join(__dirname, '../../src/lib/components/rb/wave/render.ts');

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
	// ANCHORED, not indexOf: '.perf-root {' is a trailing substring of
	// "html[data-theme='light'] .perf-root {", so a bare search would resolve
	// the dark block only for as long as theme.css keeps the dark section
	// first. Reordering the file would then check dark-labelled surfaces
	// against light-theme hex - a silently wrong test rather than a red one.
	const anchored = new RegExp(`^${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'm');
	const match = anchored.exec(themeCss);
	assert.ok(match, `theme.css must still define ${selector} at the start of a line`);
	const start = match.index;
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

/** Background declaration for one exact selector in WaveRow.svelte. */
function backgroundOf(css, selector) {
	const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	const match = new RegExp(`${escaped}\\s*\\{[^}]*?background:\\s*([^;]+);`).exec(css);
	assert.ok(match, `WaveRow must still declare ${selector} background`);
	return match[1].trim();
}

// REQ: A11Y-03
test('the dim token clears the 4.5:1 AA floor on every surface this title renders on', () => {
	const themeCss = readFileSync(themePath, 'utf8');
	const waveRowCss = readFileSync(waveRowPath, 'utf8');
	for (const selector of ['.rb-waverow.secondary', '.rb-waverow.secondary.deck-focus']) {
		const background = backgroundOf(waveRowCss, selector);
		assert.match(
			background,
			/var\(--rb-waverow-secondary\)/,
			`${selector} must use var(--rb-waverow-secondary), got ${background}`
		);
		assert.doesNotMatch(
			background,
			/#[0-9a-fA-F]{6}/,
			`${selector} must not keep a hardcoded #RRGGBB fill, got ${background}`
		);
	}

	const renderSrc = readFileSync(renderPath, 'utf8');
	const fnStart = renderSrc.indexOf('export function resolvePaintPalette');
	assert.ok(fnStart !== -1, 'resolvePaintPalette must still exist');
	const nextExport = renderSrc.indexOf('\nexport function', fnStart + 1);
	const fnChunk = renderSrc.slice(fnStart, nextExport === -1 ? undefined : nextExport);
	assert.doesNotMatch(
		fnChunk,
		/#1a1f28/i,
		'resolvePaintPalette must not hardcode the dark secondary-row hex'
	);

	const dark = paletteFor(themeCss, '.perf-root {');
	const light = paletteFor(themeCss, "html[data-theme='light'] .perf-root {");
	const surfaces = [
		['dark --rb-bg', dark['--rb-text-dim'], dark['--rb-bg']],
		['dark --rb-panel', dark['--rb-text-dim'], dark['--rb-panel']],
		['dark --rb-panel-raised', dark['--rb-text-dim'], dark['--rb-panel-raised']],
		['dark deck 3/4 row', dark['--rb-text-dim'], dark['--rb-waverow-secondary']],
		['light --rb-bg', light['--rb-text-dim'], light['--rb-bg']],
		['light --rb-panel', light['--rb-text-dim'], light['--rb-panel']],
		['light --rb-panel-raised', light['--rb-text-dim'], light['--rb-panel-raised']],
		['light deck 3/4 row', light['--rb-text-dim'], light['--rb-waverow-secondary']],
		['light deck 3/4 row primary', light['--rb-text'], light['--rb-waverow-secondary']]
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

// REQ: A11Y-03
test('resolvePaintPalette paints decks 3 and 4 with the secondary row token', async () => {
	const render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	const palette = {
		bg: '#010203',
		secondaryBg: '#fffdf8',
		grid: '#000000',
		cue: '#ffffff',
		loop: '#ffffff',
		playhead: '#ffffff'
	};
	assert.deepEqual(render.resolvePaintPalette(1, palette), palette);
	assert.deepEqual(render.resolvePaintPalette(2, palette), palette);
	const deck3 = render.resolvePaintPalette(3, palette);
	assert.equal(deck3.bg, palette.secondaryBg);
	assert.notEqual(deck3.bg, palette.bg);
	const deck4 = render.resolvePaintPalette(4, palette);
	assert.equal(deck4.bg, palette.secondaryBg);
	const alreadySecondary = { ...palette, bg: palette.secondaryBg };
	assert.deepEqual(render.resolvePaintPalette(3, alreadySecondary), alreadySecondary);
});
