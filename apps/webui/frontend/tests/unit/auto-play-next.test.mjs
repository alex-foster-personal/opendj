import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let findRepetitiveLoopWindow;
let loopWindowToMs;
let planAutoPlayNextBeatLoop;
let bassEntryMs;
let approximateDropMs;
let duckedLowEqKnob;
let DEFAULT_AUTO_PLAY_NEXT_CONFIG;

before(async () => {
	({
		findRepetitiveLoopWindow,
		loopWindowToMs,
		planAutoPlayNextBeatLoop,
		bassEntryMs,
		approximateDropMs,
		duckedLowEqKnob,
		DEFAULT_AUTO_PLAY_NEXT_CONFIG
	} = await loadTypeScriptModule('src/lib/rb/auto-play-next.ts'));
});

// 16 beats, 4/4 @ 128bpm (~0.469s/beat), so two adjacent 8-beat windows.
const DURATION_SEC = 8 * 0.469 * 2;
function _beats(n) {
	const out = [];
	for (let i = 0; i < n; i++) out.push({ n: (i % 4) + 1, bpm: 128, t: i * 0.469 });
	return out;
}
const BEATS_16 = _beats(16);

function _flatWaveform(length, low, mid, high) {
	return {
		kind: 'tri',
		preview: { length: 1, low: [0], mid: [0], high: [0] },
		detail: {
			length,
			low: new Array(length).fill(low),
			mid: new Array(length).fill(mid),
			high: new Array(length).fill(high)
		}
	};
}

test('findRepetitiveLoopWindow finds the later armable 8-beat window on a downbeat', () => {
	const beats32 = _beats(32);
	const durationSec = 8 * 0.469 * 4;
	const waveform = _flatWaveform(3200, 0.4, 0.3, 0.2);
	const window = findRepetitiveLoopWindow(waveform, beats32, durationSec, DEFAULT_AUTO_PLAY_NEXT_CONFIG);
	assert.deepEqual(window, { startBeatIdx: 16, endBeatIdx: 24 });
});

test('findRepetitiveLoopWindow returns null when the two windows are dissimilar', () => {
	const waveform = {
		kind: 'tri',
		preview: { length: 1, low: [0], mid: [0], high: [0] },
		detail: {
			length: 1600,
			low: new Array(1600).fill(0).map((_, i) => (i < 800 ? 0.1 : 0.9)),
			mid: new Array(1600).fill(0.3),
			high: new Array(1600).fill(0.2)
		}
	};
	const window = findRepetitiveLoopWindow(waveform, BEATS_16, DURATION_SEC, DEFAULT_AUTO_PLAY_NEXT_CONFIG);
	assert.equal(window, null);
});

test('findRepetitiveLoopWindow degrades to null rather than throwing on too few beats', () => {
	const waveform = _flatWaveform(100, 0.4, 0.3, 0.2);
	assert.equal(findRepetitiveLoopWindow(waveform, _beats(4), 2, DEFAULT_AUTO_PLAY_NEXT_CONFIG), null);
	assert.throws(() => findRepetitiveLoopWindow(waveform, [], 2, DEFAULT_AUTO_PLAY_NEXT_CONFIG), /beats must be non-empty/);
});

test('loopWindowToMs maps a beat-index window to the grid\'s own ms timestamps', () => {
	const ms = loopWindowToMs(BEATS_16, { startBeatIdx: 8, endBeatIdx: 16 }, DURATION_SEC);
	assert.equal(ms.in_ms, BEATS_16[8].t * 1000);
	assert.equal(ms.out_ms, DURATION_SEC * 1000);
});

test('bassEntryMs finds the first low-energy crossing at or after fromMs', () => {
	const length = 1000;
	const waveform = {
		kind: 'tri',
		preview: { length: 1, low: [0], mid: [0], high: [0] },
		detail: {
			length,
			low: new Array(length).fill(0).map((_, i) => (i < 500 ? 0.1 : 0.9)),
			mid: new Array(length).fill(0.3),
			high: new Array(length).fill(0.2)
		}
	};
	const durationSec = 10;
	const ms = bassEntryMs(waveform, BEATS_16, durationSec, 0, DEFAULT_AUTO_PLAY_NEXT_CONFIG);
	assert.ok(ms !== null && Math.abs(ms - 5000) < 50, `expected ~5000ms, got ${ms}`);
});

test('bassEntryMs returns null when the bass never crosses the threshold', () => {
	const waveform = _flatWaveform(1000, 0.05, 0.3, 0.2);
	assert.equal(bassEntryMs(waveform, BEATS_16, 10, 0, DEFAULT_AUTO_PLAY_NEXT_CONFIG), null);
});

test('approximateDropMs lands dropApproxBeats beats after the nearest beat to bass entry', () => {
	// Bass enters near beat[2].t; the drop is dropApproxBeats(8) beats later -> beat[10].
	const ms = approximateDropMs(BEATS_16, BEATS_16[2].t * 1000, DEFAULT_AUTO_PLAY_NEXT_CONFIG);
	assert.equal(ms, BEATS_16[10].t * 1000);
});

test('approximateDropMs returns null once the approximated drop runs past the grid', () => {
	const ms = approximateDropMs(BEATS_16, BEATS_16[15].t * 1000, DEFAULT_AUTO_PLAY_NEXT_CONFIG);
	assert.equal(ms, null);
});

test('duckedLowEqKnob cuts the requested fraction from flat (0.5)', () => {
	assert.equal(duckedLowEqKnob(0), 0.5);
	assert.equal(duckedLowEqKnob(0.3), 0.35);
	assert.equal(duckedLowEqKnob(1), 0);
	assert.throws(() => duckedLowEqKnob(1.5), /fraction must be within 0..1/);
});

// PLAY-11 / issue #3532: downbeat-aligned beat_loop planner.
test('fc60002b81a8: planAutoPlayNextBeatLoop snaps a non-downbeat window start to the next n===1 beat', () => {
	const beats = _beats(32);
	const window = { startBeatIdx: 9, endBeatIdx: 17 };
	const plan = planAutoPlayNextBeatLoop(beats, window, DURATION_SEC * 2);
	assert.notEqual(plan, null);
	assert.equal(plan.beats, 8);
	assert.equal(plan.start_ms, beats[12].t * 1000);
	assert.equal(beats[12].n, 1);
});

test('planAutoPlayNextBeatLoop returns null when no downbeat-aligned 8-beat span fits', () => {
	const beats = _beats(16);
	const plan = planAutoPlayNextBeatLoop(beats, { startBeatIdx: 13, endBeatIdx: 21 }, DURATION_SEC);
	assert.equal(plan, null);
});
