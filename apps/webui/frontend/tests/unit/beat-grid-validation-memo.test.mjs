import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// PERF-GRID-01 regression lines:
// - if validateBeatGrid walks a grid it already validated then broken
// - if a replaced grid (new array) skips validation then broken
// - if a bad grid stops throwing on a repeat call then broken
// - if the memoized beatPeriodS returns another grid's period then broken
//
// Measured on demon-llama, Mon 5 Oct 2026, two synced decks playing in
// headless Chromium: validateBeatGrid was 56% of main-thread time, called on
// every frame from the deck snapshot BPM, BeatJump and the wave row.

let math;
let render;

const FRAMES = 600; // ten seconds of 60 Hz paint

function grid(length = 1000) {
	return Array.from({ length }, (_, i) => ({ n: (i % 4) + 1, bpm: 124, t: 0.1 + i * (60 / 124) }));
}

before(async () => {
	math = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
});

test('one grid is walked once across a burst of per-frame validations', () => {
	math.resetBeatGridValidationMemoForTest();
	const beats = grid();
	for (let frame = 0; frame < FRAMES; frame++) math.validateBeatGrid(beats);
	assert.equal(math.beatGridValidationWalksForTest(), 1, `${FRAMES} frames must cost one walk`);
});

test('the frame-time helpers that validate internally share the memo', () => {
	math.resetBeatGridValidationMemoForTest();
	const beats = grid();
	for (let frame = 0; frame < FRAMES; frame++) {
		math.quantizeToNearestBeat(beats, 30 + frame / 60);
		math.nextDownbeatAtOrAfter(beats, 30 + frame / 60);
	}
	assert.equal(math.beatGridValidationWalksForTest(), 1);
});

test('a replaced grid is validated again, even with identical content', () => {
	math.resetBeatGridValidationMemoForTest();
	math.validateBeatGrid(grid());
	math.validateBeatGrid(grid());
	assert.equal(math.beatGridValidationWalksForTest(), 2, 'identity, not content, is the grid version');
});

test('a grid whose length changed is validated again', () => {
	math.resetBeatGridValidationMemoForTest();
	const beats = grid(8);
	math.validateBeatGrid(beats);
	beats.push({ n: 2, bpm: 124, t: 100 }); // n cadence broken: 1,2,3,4,1,2,3,4,2
	assert.throws(() => math.validateBeatGrid(beats), /cadence/);
	assert.equal(math.beatGridValidationWalksForTest(), 2);
});

test('a bad grid throws on every call but is walked once', () => {
	math.resetBeatGridValidationMemoForTest();
	const beats = grid();
	beats[900] = { n: beats[900].n, bpm: 124, t: beats[899].t }; // not strictly increasing
	for (let frame = 0; frame < FRAMES; frame++) {
		assert.throws(() => math.validateBeatGrid(beats), /strictly increasing/);
	}
	assert.equal(math.beatGridValidationWalksForTest(), 1);
});

test('the memo never accepts what the walk rejects (control)', () => {
	math.resetBeatGridValidationMemoForTest();
	assert.throws(() => math.validateBeatGrid([{ n: 1, bpm: 124, t: 0 }]), /at least 2 beats/);
	assert.throws(() => math.validateBeatGrid([{ n: 1, bpm: 124, t: 0 }, { n: 3, bpm: 124, t: 1 }]), /cadence/);
	assert.doesNotThrow(() => math.validateBeatGrid([{ n: 1, bpm: 124, t: 0 }, { n: 2, bpm: 124, t: 1 }]));
});

test('beatPeriodS is the grid median and is stable for one grid', () => {
	const beats = grid();
	const first = render.beatPeriodS(beats);
	assert.ok(Math.abs(first - 60 / 124) < 1e-9);
	for (let frame = 0; frame < FRAMES; frame++) assert.equal(render.beatPeriodS(beats), first);
	const slower = Array.from({ length: 16 }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: i * 0.5 }));
	assert.equal(render.beatPeriodS(slower), 0.5, 'a different grid gets its own period');
});
