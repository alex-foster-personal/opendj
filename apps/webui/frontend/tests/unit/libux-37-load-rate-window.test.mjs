/**
 * LIBUX-37: the library load indicator's rows/s figure is measured over a
 * minimum window and is absent until it means something.
 *
 * Measured Thu 1 Oct 2026 on the live preview: "178571 rows/s" on the first
 * chunk. The clock started when the first page (500 rows) arrived and the
 * figure divided ALL rows received by the time since then, so a second
 * update 2.8 ms later read 500 / 0.0028 s. The same sum overstated every
 * later figure too, because the first page was counted and its time was not.
 *
 * Regression lines:
 *   - if a rate is shown before one second of measured window then broken
 *   - if the first page's rows are counted without the time they took then broken
 *   - if a stalled load shows a made-up rate instead of nothing or a falling one then broken
 *   - if the rate readout loses its hover title then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const INDICATOR = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/LibraryLoadIndicator.svelte', import.meta.url)
);

let rate;

before(async () => {
	rate = await loadTypeScriptModule('src/lib/rb/load-rate.ts');
});

test('the measured failure: a second update 2.8 ms after the first page shows no rate', () => {
	const baseline = { atMs: 1000, loaded: 500 };
	// Before: 500 rows / 0.0028 s = 178,571 rows/s.
	assert.equal(Math.round(500 / 0.0028), 178571, 'control: the reported figure is this sum');
	assert.equal(rate.loadRowsPerSecond(baseline, 1002.8, 500), null);
});

test('no rate until the window is at least one second long', () => {
	const baseline = { atMs: 0, loaded: 500 };
	assert.equal(rate.LOAD_RATE_MIN_WINDOW_MS, 1000);
	assert.equal(rate.loadRowsPerSecond(baseline, 999, 1000), null);
	assert.equal(rate.loadRowsPerSecond(baseline, 1000, 1000), 500);
});

test('the rate counts only rows that arrived inside the window', () => {
	// 500 rows at the baseline, 2,500 more over the next 5 s: 500 rows/s,
	// not the 600 rows/s the earlier all-rows sum reported.
	assert.equal(rate.loadRowsPerSecond({ atMs: 2000, loaded: 500 }, 7000, 3000), 500);
});

test('overshoot control: a real rate is still shown once it is measurable, and a stall is not', () => {
	const baseline = { atMs: 0, loaded: 500 };
	assert.equal(rate.loadRowsPerSecond(baseline, 4000, 2500), 500);
	assert.equal(rate.loadRowsPerSecond(baseline, 4000, 500), null, 'no rows in the window is no rate, not 0 rows/s');
});

test('the indicator renders the shared figure, keeps its hover title, and hides a null rate', () => {
	const src = readFileSync(INDICATOR, 'utf8');
	assert.match(src, /loadRowsPerSecond\(/);
	assert.doesNotMatch(src, /p\.loaded \/ elapsedS/, 'the all-rows-over-elapsed sum must be gone');
	assert.match(src, /\{#if rowsPerSecond !== null\}<span class="lli-rate" title="[^"]+"/);
});
