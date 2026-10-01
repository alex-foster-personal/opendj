/**
 * WaveRow.chrome.css is pulled into WaveRow.svelte's <style> with @import.
 * Svelte does not scope an imported stylesheet: Vite inlines it as GLOBAL CSS
 * once the component loads. A bare `canvas { position: absolute; inset: 0 }`
 * there restyled every canvas in the app (the built CSS carried exactly that
 * rule). Every rule in the file must therefore be rooted at the component's
 * own `.rb-waverow` element.
 *
 * Regression lines:
 * - if any selector in WaveRow.chrome.css is not rooted at .rb-waverow then the
 *   file leaks global styles into every page that mounts a WaveRow
 * - if the rule for the seek canvas matches a bare canvas element then every
 *   canvas in the app is stretched over its parent
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const css = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/wave/WaveRow.chrome.css', import.meta.url)),
	'utf8'
).replace(/\/\*[\s\S]*?\*\//g, '');

const selectors = [...css.matchAll(/([^{}]+)\{[^}]*\}/g)].flatMap((m) =>
	m[1].split(',').map((s) => s.trim())
);

test('WaveRow.chrome.css declares rules (the scan is not reading an empty file)', () => {
	assert.ok(selectors.length >= 5, `expected the WaveRow chrome rules, found ${selectors.length}`);
});

test('every WaveRow.chrome.css selector is rooted at .rb-waverow', () => {
	const unrooted = selectors.filter((s) => !/^\.rb-waverow(\b|[.\s:>])/.test(s));
	assert.deepEqual(unrooted, [], `unscoped global selectors: ${unrooted.join(' | ')}`);
});

test('no WaveRow.chrome.css selector ends in a bare element (canvas, span, div)', () => {
	const bare = selectors.filter((s) => /(^|[\s>+~])[a-z]+$/.test(s));
	assert.deepEqual(bare, [], `selectors ending in a bare element: ${bare.join(' | ')}`);
});
