// requirement: DECKGAP-7691g
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let findRepetitiveLoopWindow;
let findAutoPlayNextLoopWindow;
let autoPlayMixTrackInput;
let loopWindowToMs;
let planAutoPlayNextBeatLoop;
let bassEntryMs;
let approximateDropMs;
let duckedLowEqKnob;
let DEFAULT_AUTO_PLAY_NEXT_CONFIG;

before(async () => {
	({
		findRepetitiveLoopWindow,
		findAutoPlayNextLoopWindow,
		autoPlayMixTrackInput,
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
	const window = findRepetitiveLoopWindow(
		waveform,
		beats32,
		durationSec,
		DEFAULT_AUTO_PLAY_NEXT_CONFIG
	);
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
	const window = findRepetitiveLoopWindow(
		waveform,
		BEATS_16,
		DURATION_SEC,
		DEFAULT_AUTO_PLAY_NEXT_CONFIG
	);
	assert.equal(window, null);
});

test('findRepetitiveLoopWindow degrades to null rather than throwing on too few beats', () => {
	const waveform = _flatWaveform(100, 0.4, 0.3, 0.2);
	assert.equal(
		findRepetitiveLoopWindow(waveform, _beats(4), 2, DEFAULT_AUTO_PLAY_NEXT_CONFIG),
		null
	);
	assert.throws(
		() => findRepetitiveLoopWindow(waveform, [], 2, DEFAULT_AUTO_PLAY_NEXT_CONFIG),
		/beats must be non-empty/
	);
});

// Pin 9f05ca7e3f6f. 160 beats = 40 bars, so B-16 is beat 96 (64 beats remain).
// The old tail search would arm beat 144.
const LONG_BEATS = 160;
const LONG_DURATION_SEC = LONG_BEATS * 0.469;
const FLAT_PROFILE = [0.4, 0.3, 0.2];

function _detailForBeats(beats, durationSec, valueAtBeat) {
	const length = beats.length * 10;
	const low = new Array(length).fill(0);
	const mid = new Array(length).fill(0);
	const high = new Array(length).fill(0);
	const scale = length / Math.max(durationSec, 1e-6);
	for (let beatIdx = 0; beatIdx < beats.length; beatIdx++) {
		const t0 = beats[beatIdx].t;
		const t1 = beatIdx + 1 < beats.length ? beats[beatIdx + 1].t : durationSec;
		const lo = Math.round(t0 * scale);
		const hi = Math.round(t1 * scale);
		const [l, m, h] = valueAtBeat(beatIdx);
		for (let i = Math.max(0, lo); i < Math.min(length, hi); i++) {
			low[i] = l;
			mid[i] = m;
			high[i] = h;
		}
	}
	return {
		kind: 'tri',
		preview: { length: 1, low: [0], mid: [0], high: [0] },
		detail: { length, low, mid, high }
	};
}

function _alternatingBar(beatIdx) {
	return Math.floor(beatIdx / 4) % 2 === 0 ? [0.9, 0.1, 0.1] : [0.1, 0.1, 0.9];
}

test('9f05ca7e3f6f: a long grid loops at B-16 instead of the tail', () => {
	const beats = _beats(LONG_BEATS);
	const waveform = _detailForBeats(beats, LONG_DURATION_SEC, () => FLAT_PROFILE);
	const window = findRepetitiveLoopWindow(
		waveform,
		beats,
		LONG_DURATION_SEC,
		DEFAULT_AUTO_PLAY_NEXT_CONFIG
	);
	assert.deepEqual(window, { startBeatIdx: 96, endBeatIdx: 104 });
});

test('9f05ca7e3f6f: picks the nearby downbeat whose 4 beats before and after match', () => {
	const beats = _beats(LONG_BEATS);
	const seam = 80;
	const waveform = _detailForBeats(beats, LONG_DURATION_SEC, (beatIdx) => {
		if (beatIdx >= seam - 4 && beatIdx < seam + 4) return FLAT_PROFILE;
		return _alternatingBar(beatIdx);
	});
	const window = findRepetitiveLoopWindow(
		waveform,
		beats,
		LONG_DURATION_SEC,
		DEFAULT_AUTO_PLAY_NEXT_CONFIG
	);
	assert.deepEqual(window, { startBeatIdx: seam, endBeatIdx: seam + 8 });
});

test('9f05ca7e3f6f: falls back to B-16 when no nearby 4-beat seam matches', () => {
	const beats = _beats(LONG_BEATS);
	const waveform = _detailForBeats(beats, LONG_DURATION_SEC, _alternatingBar);
	const window = findRepetitiveLoopWindow(
		waveform,
		beats,
		LONG_DURATION_SEC,
		DEFAULT_AUTO_PLAY_NEXT_CONFIG
	);
	assert.deepEqual(window, { startBeatIdx: 96, endBeatIdx: 104 });
});

test("loopWindowToMs maps a beat-index window to the grid's own ms timestamps", () => {
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

test('[if] phrase mix-out exists [then] findAutoPlayNextLoopWindow starts on n===1', () => {
	const beats = _beats(128);
	const durationSec = 128 * 0.469;
	const waveform = _flatWaveform(4000, 0.4, 0.3, 0.2);
	const phrases = [];
	let bar = 0;
	const kinds = [1, 2, 5, 3, 2, 5, 6];
	const barsEach = [16, 16, 32, 16, 16, 16, 16];
	for (let i = 0; i < kinds.length; i += 1) {
		const start = bar * 4 * 0.469;
		bar += barsEach[i];
		phrases.push({ start_s: start, end_s: bar * 4 * 0.469, kind: kinds[i], mood: 1 });
	}
	const mixInput = autoPlayMixTrackInput(beats, durationSec, phrases, 'rekordbox');
	const window = findAutoPlayNextLoopWindow(
		waveform,
		beats,
		durationSec,
		DEFAULT_AUTO_PLAY_NEXT_CONFIG,
		mixInput
	);
	assert.notEqual(window, null);
	assert.equal(beats[window.startBeatIdx].n, 1);
	assert.equal(window.endBeatIdx - window.startBeatIdx, 8);
	assert.notEqual(window.mix_out_bar, null);
});

test('[if] phrases are absent [then] findAutoPlayNextLoopWindow keeps B-16 fallback', () => {
	const beats = _beats(160);
	const durationSec = LONG_DURATION_SEC;
	const waveform = _detailForBeats(beats, durationSec, () => FLAT_PROFILE);
	const empty = autoPlayMixTrackInput(beats, durationSec, [], 'none');
	const window = findAutoPlayNextLoopWindow(
		waveform,
		beats,
		durationSec,
		DEFAULT_AUTO_PLAY_NEXT_CONFIG,
		empty
	);
	assert.deepEqual(window, { startBeatIdx: 96, endBeatIdx: 104, mix_out_bar: null });
});
