/**
 * Playheads snap to the device pixel grid so a playing deck rewrites their
 * style only when they move (two decks playing dropped frames, Fri 9 Oct 2026).
 *
 * The helper is exercised directly, then each of the three playheads that
 * moved every frame is checked to route through it, so a later edit that
 * goes back to the raw fraction fails here rather than on the Air.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let snapFractionToPixels;

before(async () => {
	({ snapFractionToPixels } = await loadTypeScriptModule('src/lib/ui/pixel-snap.ts'));
});

test('a playing deck holds one value until the marker crosses a device pixel', () => {
	// 330 device px (165 px strip at 2x) over a 300 s track: one pixel per ~0.9 s.
	const px = 330;
	const durationMs = 300_000;
	const frameMs = 1000 / 60;
	const values = [];
	for (let t = 60_000; t < 61_000; t += frameMs) values.push(snapFractionToPixels(t / durationMs, px));
	const distinct = new Set(values);
	assert.ok(values.length >= 59, `expected about 60 frames, got ${values.length}`);
	assert.ok(distinct.size <= 2, `a second of play changed the value ${distinct.size} times`);
	// Control: the raw fraction changes on every frame, so equality is the snap's doing.
	const raw = new Set();
	for (let t = 60_000; t < 61_000; t += frameMs) raw.add(t / durationMs);
	assert.equal(raw.size, values.length);
});

test('the snapped value lands on the pixel grid and within half a pixel', () => {
	for (const [fraction, px] of [[0.123456, 330], [0.5, 200], [0.999, 371.5], [0.0004, 1000]]) {
		const snapped = snapFractionToPixels(fraction, px);
		const onGrid = snapped * px;
		assert.ok(Math.abs(onGrid - Math.round(onGrid)) < 1e-9, `${snapped} is off the grid of ${px}`);
		assert.ok(Math.abs(snapped - fraction) * px <= 0.5 + 1e-9, `${fraction} moved more than half a pixel`);
	}
});

test('the marker still moves: it is not frozen at its first position', () => {
	const px = 330;
	assert.notEqual(snapFractionToPixels(0.1, px), snapFractionToPixels(0.2, px));
	assert.equal(snapFractionToPixels(0, px), 0);
	assert.equal(snapFractionToPixels(1, px), 1);
});

test('out-of-range fractions clamp to the ends', () => {
	assert.equal(snapFractionToPixels(-0.3, 330), 0);
	assert.equal(snapFractionToPixels(1.7, 330), 1);
});

test('a NaN or infinite fraction throws instead of passing through or freezing at an end', () => {
	for (const fraction of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
		assert.throws(() => snapFractionToPixels(fraction, 330), RangeError, `fraction ${fraction}`);
		assert.throws(() => snapFractionToPixels(fraction, null), RangeError, `fraction ${fraction}, unmeasured size`);
	}
});

test('an unmeasured size (null or 0) draws unsnapped rather than at zero', () => {
	for (const px of [null, 0]) {
		assert.equal(snapFractionToPixels(0.4321, px), 0.4321, `size ${px}`);
	}
	assert.equal(snapFractionToPixels(1.5, 0), 1);
});

test('a broken size (NaN, infinite, negative) throws instead of silently unsnapping', () => {
	for (const px of [-5, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
		assert.throws(() => snapFractionToPixels(0.4321, px), RangeError, `size ${px}`);
	}
});

for (const [file, binding] of [
	['src/lib/components/rb/browser/PreviewStrip.svelte', 'const nowX'],
	['src/lib/components/rb/deck/StripWaveform.svelte', 'const posPct'],
	['src/lib/components/rb/deck/JogDial.svelte', 'const tickAngle']
]) {
	test(`${binding} in ${file.split('/').pop()} snaps to the pixel grid`, () => {
		const src = readFileSync(new URL(`../../${file}`, import.meta.url), 'utf8');
		const start = src.indexOf(binding);
		assert.ok(start >= 0, `${binding} not found in ${file}`);
		const body = src.slice(start, src.indexOf(');', start));
		assert.match(body, /snapFractionToPixels\(/, `${binding} no longer snaps`);
	});
}
