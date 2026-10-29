import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// 'blocks' timing bound (ADR: deck waveforms keep peak detail).
// Regression lines:
// - if a Blocks bar's left edge ever lands LATER than the transient it shows then broken
// - if a Blocks bar's left edge lands more than one BLOCK_PITCH_PX EARLIER than its transient then broken
//   (inclusive: a point exactly on a block boundary also lights the block before it via float
//   rounding in blockHeights' bucket end, so exactly one pitch early is reachable)
// - if a single-point transient paints no block at all (averaged or point-sampled away) then broken
// - if two decks showing the same hit can differ by more than one pitch then broken

let render;

before(async () => {
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
});

const WIDTH_PX = 1200;
const POINTS = 3600; // denser than the pixels, like real detail data at deck zoom
const NORMS = { low: 1, mid: 1, high: 1 };

function spikeAt(index) {
	const low = new Float32Array(POINTS);
	low[index] = 1;
	return { length: POINTS, low, mid: new Float32Array(POINTS), high: new Float32Array(POINTS) };
}

/** Left edge in px of the first block painted for a lone spike at `index`. */
function firstLitBlockLeftPx(index) {
	const heights = render.blockHeights(spikeAt(index), WIDTH_PX, NORMS, 'max', null, 'true-peak');
	const lit = heights.findIndex((h) => h > 0);
	assert.ok(lit >= 0, `spike at point ${index} painted no block`);
	return lit * render.BLOCK_PITCH_PX;
}

function spikeStartPx(index) {
	return (index / POINTS) * WIDTH_PX;
}

test('every lone transient paints a block whose left edge is never late and at most one pitch early', () => {
	const pitch = render.BLOCK_PITCH_PX;
	let checked = 0;
	for (let i = 0; i < POINTS; i++) {
		const earlyPx = spikeStartPx(i) - firstLitBlockLeftPx(i);
		assert.ok(earlyPx >= -1e-9, `spike at point ${i}: block is ${-earlyPx}px LATE`);
		assert.ok(earlyPx <= pitch + 1e-9, `spike at point ${i}: block is ${earlyPx}px early, bound is <= ${pitch}`);
		checked++;
	}
	assert.equal(checked, POINTS);
});

test('two decks showing the same hit differ by at most one pitch, in either direction', () => {
	const pitch = render.BLOCK_PITCH_PX;
	let worst = 0;
	for (let i = 0; i < POINTS; i++) {
		const earlyA = spikeStartPx(i) - firstLitBlockLeftPx(i);
		for (const j of [i + 1, i + 2]) {
			if (j >= POINTS) continue;
			// Same hit on deck B, landing a fraction of a pixel later in its own grid.
			const earlyB = spikeStartPx(j) - firstLitBlockLeftPx(j);
			worst = Math.max(worst, Math.abs(earlyA - earlyB));
		}
	}
	assert.ok(worst > 0, 'control: neighbouring hits never disagreed, the probe is not measuring offset');
	assert.ok(worst <= pitch + 1e-9, `relative offset reached ${worst}px, bound is <= ${pitch}`);
});
