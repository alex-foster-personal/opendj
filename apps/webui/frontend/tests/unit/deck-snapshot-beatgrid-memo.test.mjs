import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';
import v8 from 'node:v8';
import { runInNewContext } from 'node:vm';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * LATENCY-01 drag weight: queryPerformanceState() must not rebuild every deck's
 * beatgrid on every command.
 *
 * It runs after EVERY command, including the null-scope continuous ones - trim,
 * EQ, faders, crossfader - which fire once per pointermove. It rebuilt all four
 * decks' beatgrids each time: two full array maps per deck, one allocating an
 * object per beat. Measured 3.9ms p50 / 5.4ms worst with four decks loaded
 * (2221 beats), i.e. 42-65% of the main thread at a 120Hz pointer rate, spent
 * rebuilding values that only change on load. It never delayed the audio, but
 * it is what makes a knob drag feel heavy, and it grows as decks fill.
 *
 * The memo is keyed by ANLZ object IDENTITY, which is safe precisely because
 * the engine REPLACES st.anlz rather than mutating it. That makes the dangerous
 * failure "a stale grid outlives its track", so these tests spend most of their
 * effort on invalidation rather than on the happy path.
 *
 * Regression lines:
 * - if the projection is rebuilt per query then a knob drag pays for four decks
 *   of beatgrid allocation on every pointermove
 * - if the memo is not invalidated when anlz changes identity then a newly
 *   loaded deck serves the previous track's beatgrid to every IPC consumer
 * - if one deck's projection can be served for another then decks cross-talk
 * - if the shared arrays are not frozen then one consumer mutating a snapshot
 *   poisons every later read
 * - if the IPC payload shape changes then agent consumers break
 */

let ipc;

/** Minimal ANLZ payload: only the beatgrid is projected into the snapshot. */
function fakeAnlz(beats) {
	return { beatgrid: { beats: beats.map((t, n) => ({ n, bpm: 120, t })) } };
}

before(async () => {
	ipc = await loadTypeScriptModule('tests/unit/fixtures/perf-ipc-entry.ts');
});

beforeEach(() => {
	for (const deck of [1, 2, 3, 4]) ipc.deckStates[deck].anlz = null;
});

//-----------------------------------------------------------------------------
// the projection is computed once
//-----------------------------------------------------------------------------

test('repeat queries reuse the projection instead of rebuilding it', () => {
	ipc.deckStates[1].anlz = fakeAnlz([0, 0.5, 1]);

	const first = ipc.queryPerformanceState().decks[1];
	const second = ipc.queryPerformanceState().decks[1];

	assert.equal(
		first.beatgrid,
		second.beatgrid,
		'if the beatgrid array is a new object every query then every pointermove is ' +
			'paying to rebuild a value that only changes on load'
	);
	assert.equal(first.beatgrid_ms, second.beatgrid_ms);
});

test('the projected values are exactly what they were before the memo', () => {
	ipc.deckStates[2].anlz = fakeAnlz([0, 0.5, 1.25]);
	const snapshot = ipc.queryPerformanceState().decks[2];

	assert.deepEqual(snapshot.beatgrid_ms, [0, 500, 1250]);
	assert.deepEqual(snapshot.beatgrid, [
		{ n: 0, bpm: 120, time_ms: 0 },
		{ n: 1, bpm: 120, time_ms: 500 },
		{ n: 2, bpm: 120, time_ms: 1250 }
	]);
	assert.equal(
		JSON.stringify(snapshot.beatgrid_ms),
		'[0,500,1250]',
		'the IPC payload must serialize identically - agents read this shape'
	);
});

test('an empty deck projects empty arrays, not null', () => {
	const snapshot = ipc.queryPerformanceState().decks[3];
	assert.deepEqual(snapshot.beatgrid, []);
	assert.deepEqual(snapshot.beatgrid_ms, []);
});

//-----------------------------------------------------------------------------
// invalidation: the dangerous half
//-----------------------------------------------------------------------------

test('a new ANLZ payload invalidates the memo rather than serving the old grid', () => {
	ipc.deckStates[1].anlz = fakeAnlz([0, 0.5]);
	assert.deepEqual(ipc.queryPerformanceState().decks[1].beatgrid_ms, [0, 500]);

	// A load or a hot-cue refresh REPLACES st.anlz; identity is what changes.
	ipc.deckStates[1].anlz = fakeAnlz([0, 0.25, 0.5, 0.75]);
	assert.deepEqual(
		ipc.queryPerformanceState().decks[1].beatgrid_ms,
		[0, 250, 500, 750],
		'if identity does not invalidate then a freshly loaded deck serves the previous ' +
			"track's beatgrid, and every consumer draws the wrong grid"
	);
});

test('unloading a deck clears its grid instead of leaving the last one cached', () => {
	ipc.deckStates[4].anlz = fakeAnlz([0, 0.5]);
	assert.equal(ipc.queryPerformanceState().decks[4].beatgrid_ms.length, 2);
	ipc.deckStates[4].anlz = null;
	assert.deepEqual(ipc.queryPerformanceState().decks[4].beatgrid_ms, []);
});

test('a payload with an identical shape but a new identity still invalidates', () => {
	// Deep-equal but not identical: the memo must not be fooled into holding the
	// old object, or a re-analysis that changes nothing visible would still leave
	// two decks sharing one array.
	ipc.deckStates[2].anlz = fakeAnlz([0, 0.5]);
	const before = ipc.queryPerformanceState().decks[2].beatgrid_ms;
	ipc.deckStates[2].anlz = fakeAnlz([0, 0.5]);
	const after = ipc.queryPerformanceState().decks[2].beatgrid_ms;
	assert.deepEqual([...before], [...after]);
	assert.notEqual(before, after, 'a replaced payload must produce a replaced projection');
});

test('decks never share a projection, even with identical grids', () => {
	ipc.deckStates[1].anlz = fakeAnlz([0, 0.5]);
	ipc.deckStates[2].anlz = fakeAnlz([0, 0.5]);
	const state = ipc.queryPerformanceState();
	assert.deepEqual([...state.decks[1].beatgrid_ms], [...state.decks[2].beatgrid_ms]);
	assert.notEqual(
		state.decks[1].beatgrid_ms,
		state.decks[2].beatgrid_ms,
		'if two decks share one array then unloading one would empty the other'
	);
});

//-----------------------------------------------------------------------------
// a shared array must not be a poisonable one
//-----------------------------------------------------------------------------

test('the shared projection is frozen, so a consumer cannot poison later reads', () => {
	ipc.deckStates[1].anlz = fakeAnlz([0, 0.5]);
	const snapshot = ipc.queryPerformanceState().decks[1];

	assert.ok(Object.isFrozen(snapshot.beatgrid_ms), 'the ms array must be frozen');
	assert.ok(Object.isFrozen(snapshot.beatgrid), 'the beat row array must be frozen');
	assert.ok(Object.isFrozen(snapshot.beatgrid[0]), 'each beat row must be frozen too');

	// Before the memo these arrays were rebuilt per call, so a mutation was
	// harmless. Now they are shared, so it must fail loudly rather than corrupt
	// every later snapshot.
	assert.throws(() => {
		snapshot.beatgrid_ms[0] = -1;
	}, TypeError);
	assert.throws(() => {
		snapshot.beatgrid[0].time_ms = -1;
	}, TypeError);

	assert.deepEqual(ipc.queryPerformanceState().decks[1].beatgrid_ms, [0, 500]);
});

//-----------------------------------------------------------------------------
// PERFMODE-14: the memo must not be what keeps an unloaded track alive
//-----------------------------------------------------------------------------

/** A real major GC without a CLI flag: the test runner does not pass --expose-gc. */
function collectGarbage() {
	v8.setFlagsFromString('--expose-gc');
	runInNewContext('gc')();
}

/** Load a payload on all four decks, project them once, then unload every deck
 * through the REAL `engine.dispose()` - the same call Library mode's
 * `releaseGigRuntime()` makes, and the one discussion_r4127920891 asked this
 * test to drive instead of a hand-rolled `anlz = null` loop. `engine.dispose()`
 * needs no live AudioContext (it guards on `_masterGain`/`_ctx` both being
 * nullable) and resets every deck via the same `_emptyDeckState()` path
 * production teardown uses, so this exercises the actual disposal code the
 * regression is about, not a simulation of it.
 *
 * The LOAD half still uses `fakeAnlz()`, not a real analysis fixture: ANLZ
 * parsing happens server-side (pyrekordbox) and reaches the frontend only as
 * a plain JSON-shaped object via `fetchAnlz`/`getAnlzEntry` - there is no
 * client-side ANLZ parser here to bypass, so a fabricated object of the same
 * shape the frontend actually receives is the real production input for this
 * layer, not a stand-in for one.
 *
 * Returns only WeakRefs, so the test itself holds nothing that could keep a
 * payload alive. */
async function projectFourThenUnload() {
	const payloads = [1, 2, 3, 4].map(() => fakeAnlz([0, 0.5, 1]));
	for (const [index, anlz] of payloads.entries()) ipc.deckStates[index + 1].anlz = anlz;
	const decks = ipc.queryPerformanceState().decks;
	for (const deck of [1, 2, 3, 4]) assert.equal(decks[deck].beatgrid_ms.length, 3);
	await ipc.engine.dispose();
	return payloads.map((anlz) => new WeakRef(anlz));
}

test('an unloaded ANLZ payload is collectable even when no later query runs', async () => {
	// Library mode disposes the engine and then never queries the IPC again, so
	// a memo that only drops its entry on the NEXT query held all four decks'
	// ANLZ (waveform detail as reactive proxies, plus the full-track band
	// images keyed weakly on it) for the whole Library session: 34 MB of JS
	// heap on silver on Sat 26 Sep 2026, the largest retainer found.
	const refs = await projectFourThenUnload();
	await new Promise((resolve) => setImmediate(resolve));
	collectGarbage();
	for (const [index, ref] of refs.entries()) {
		assert.equal(
			ref.deref(),
			undefined,
			`if deck ${index + 1}'s unloaded ANLZ survives a full GC then the beatgrid memo is ` +
				'retaining every track a Library-mode switch was meant to release'
		);
	}
});

test('control: a payload still on the deck is NOT collected by the memo change', async () => {
	// The opposite overshoot: a memo that forgets live payloads would pass the
	// release test above and quietly bring back the per-query rebuild.
	ipc.deckStates[3].anlz = fakeAnlz([0, 0.5]);
	const first = ipc.queryPerformanceState().decks[3].beatgrid_ms;
	await new Promise((resolve) => setImmediate(resolve));
	collectGarbage();
	assert.equal(
		ipc.queryPerformanceState().decks[3].beatgrid_ms,
		first,
		'if a live payload loses its projection across a GC then the memo is not memoizing'
	);
});
