import assert from 'node:assert/strict';
import { before, test } from 'node:test';
import v8 from 'node:v8';
import { runInNewContext } from 'node:vm';

import { loadTypeScriptModule } from './load-typescript.mjs';

// PERF-GRID-01 regression lines:
// - if validateBeatGrid walks a grid it already validated then broken
// - if a replaced grid (new array) skips validation then broken
// - if a bad grid stops throwing on a repeat call then broken
// - if the memoized beatPeriodS returns another grid's period then broken
// - if a memoized grid error keeps its callers alive then broken (P11: each
//   gridless track load pinned the previous load's analysis, ~27 MB a load)
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

/** A real major GC without a CLI flag: the test runner does not pass --expose-gc. */
function collectGarbage() {
	v8.setFlagsFromString('--expose-gc');
	runInNewContext('gc')();
}

/**
 * Validate a bad grid from a method of a fresh object, the way a Svelte flush
 * does from inside its batch, then drop the object and report whether a GC
 * could free it. The grid stays alive, so its memo entry does too.
 */
async function callerSurvivesMemo(beats) {
	let ref;
	(() => {
		const caller = {
			payload: new Array(1000).fill(0),
			run() {
				try {
					math.validateBeatGrid(beats);
				} catch {
					// the verdict is the memo's to keep, not the caller's
				}
			}
		};
		caller.run();
		ref = new WeakRef(caller);
	})();
	for (let i = 0; i < 3; i++) {
		await new Promise((resolve) => setTimeout(resolve, 0));
		collectGarbage();
	}
	return ref.deref() !== undefined;
}

test('a memoized grid error does not keep the code that called it alive', async () => {
	math.resetBeatGridValidationMemoForTest();
	const gridless = [];
	assert.equal(await callerSurvivesMemo(gridless), false, 'the memo pinned its caller through the error stack');
	assert.throws(() => math.validateBeatGrid(gridless), /at least 2 beats/, 'the memo still rethrows');
	assert.equal(math.beatGridValidationWalksForTest(), 1);
});

test('the GC probe can see a pinned caller (control)', async () => {
	// Pin the caller on purpose: the probe must report it alive, or the test
	// above would pass whether or not the memo leaks.
	const pinned = [];
	let ref;
	(() => {
		const caller = { payload: new Array(1000).fill(0) };
		pinned.push(caller);
		ref = new WeakRef(caller);
	})();
	collectGarbage();
	assert.ok(ref.deref() !== undefined);
	pinned.length = 0;
	await new Promise((resolve) => setTimeout(resolve, 0));
	collectGarbage();
	assert.equal(ref.deref(), undefined, 'and a released caller must report freed');
});
