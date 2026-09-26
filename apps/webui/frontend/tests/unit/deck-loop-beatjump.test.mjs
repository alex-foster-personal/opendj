/**
 * Deck loop-interval selector + beat jump.
 *
 * Ported from af--deck-loop-beatjump 5fdaa686 (Sat 15 Aug 2026). The jump-math
 * and dispatcher-parity tests carry over unchanged; the interval-grid tests are
 * rewritten because this branch keeps the grid's mode and window in the shared
 * loop-interval-view module rather than in component-local $state, so they can
 * assert real behaviour instead of matching component source text.
 *
 * Requirements:
 *   ✔︎ ✅ 🎯 Beat jump counts REAL PQTZ beats, never a BPM-derived offset.
 *     [if] the grid drifts off nominal BPM [then] the target is still a real beat
 *   ✔︎ ✅ 🎯 A jump past either grid end lands on the first/last real beat.
 *     [if] no grid remains in that direction [then] the control reports inert
 *   ✔︎ ✅ 🎯 Beat jump reaches the engine only via the typed dispatcher.
 *     [if] a beat_jump payload is malformed [then ⛔️] before the engine is called
 *   ✔︎ ✅ 🎯 The loop interval grid offers base,2x,4x,8x inside MIN/MAX bounds.
 *     [if] the window would exceed 512 or fall under 1 beat [then] it stops
 *   ✔︎ ✅ 🎯 Every interval-grid control has a matching typed command.
 *     [if] an agent dispatches loop_interval_mode/base [then] the view state moves
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Real PQTZ shape: 4/4 beat numbers, strictly increasing t, drifting interval
// so a BPM-derived jump would NOT agree with a grid-counted one.
const DRIFTING_GRID = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.553 },
	{ n: 1, bpm: 126, t: 2.04 },
	{ n: 2, bpm: 126, t: 2.53 },
	{ n: 3, bpm: 126, t: 3.03 },
	{ n: 4, bpm: 126, t: 3.54 },
	{ n: 1, bpm: 125, t: 4.06 }
];

/** Grid timestamps are seconds scaled by 1000, so an exact beat can land a
 * float ULP off (4.06 * 1000 = 4059.9999999999995). The engine deliberately
 * does not round - `exactBeatLoopRangeMs` carries the same artifact - so the
 * assertion carries the tolerance instead of the production code. */
function assertMs(actual, expected) {
	assert.ok(Math.abs(actual - expected) < 1e-6, `expected ${expected}ms, got ${actual}ms`);
}

let math;
let ipc;
let view;
let loops;

before(async () => {
	math = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	view = await loadTypeScriptModule('src/lib/rb/loop-interval-view.svelte.ts');
	loops = await loadTypeScriptModule('src/lib/player/transport/loops.ts');
});

// ------------------------------------------------------------- jump math

test('beat jump counts real PQTZ beats rather than a BPM-derived duration', () => {
	// From beat index 0, +4 beats must be the 5th real beat (2.04s), not
	// 0.135 + 4 * (60 / 127) = 2.025s that nominal BPM would predict.
	assertMs(math.beatJumpTargetMs(DRIFTING_GRID, 135, 4), 2040);
	assert.notEqual(math.beatJumpTargetMs(DRIFTING_GRID, 135, 4), 135 + 4 * (60_000 / 127));

	// +8 from the first beat lands on the last supplied beat.
	assertMs(math.beatJumpTargetMs(DRIFTING_GRID, 135, 8), 4060);
});

test('beat jump snaps a between-beats anchor to the nearest real beat first', () => {
	// 1.4s sits between beats 3 (1.08) and 4 (1.553); nearest is 1.553.
	assert.equal(math.beatJumpTargetMs(DRIFTING_GRID, 1400, 1), 2040);
	// 1.2s is nearer 1.08, so +1 lands on 1.553.
	assert.equal(math.beatJumpTargetMs(DRIFTING_GRID, 1200, 1), 1553);
});

test('a negative beat jump walks the grid backwards', () => {
	// Last beat (index 8) minus 4 grid beats is index 4 at 2.04s.
	assertMs(math.beatJumpTargetMs(DRIFTING_GRID, 4060, -4), 2040);
	assertMs(math.beatJumpTargetMs(DRIFTING_GRID, 2040, -1), 1553);
});

test('a live beat loop shifts by the same real PQTZ beat count as its jump', () => {
	assert.deepEqual(
		loops.shiftLiveBeatLoopRangeMs(
			DRIFTING_GRID,
			{ in_ms: 608, out_ms: 2530 },
			2,
			4000
		),
		{ in_ms: 1553, out_ms: 3540 }
	);
	assert.deepEqual(
		loops.shiftLiveBeatLoopRangeMs(
			DRIFTING_GRID,
			{ in_ms: 608, out_ms: 2530 },
			-1,
			4000
		),
		{ in_ms: 135, out_ms: 2040 }
	);
	assert.throws(
		() => loops.shiftLiveBeatLoopRangeMs(DRIFTING_GRID, { in_ms: 2040, out_ms: 4060 }, 1, 5000),
		/does not fit/i
	);
	assert.throws(
		() => loops.shiftLiveBeatLoopRangeMs(DRIFTING_GRID, { in_ms: 608, out_ms: 2530 }, 2, 3000),
		/decoded duration/i,
		'a grid beyond decoded audio must not produce a loop the engine clips'
	);
	assert.throws(
		() => loops.shiftLiveBeatLoopRangeMs(DRIFTING_GRID, { in_ms: -1, out_ms: 2530 }, 2, 4000),
		/finite 0 <= in_ms < out_ms/i
	);
	assert.throws(
		() => loops.shiftLiveBeatLoopRangeMs(DRIFTING_GRID, { in_ms: 372, out_ms: 2530 }, -1, 4000),
		/empty, reversed, or negative/i,
		'a preserved negative endpoint offset must not move a shifted loop before zero'
	);
	assert.throws(
		() => loops.shiftLiveBeatLoopRangeMs(DRIFTING_GRID, { in_ms: 608, out_ms: 2531 }, 2, 3540),
		/exceeds decoded duration/i,
		'a preserved positive endpoint offset must not move a shifted loop past duration'
	);
});

// pin 334a50710ef0 defect B: a manually-set, non-grid-aligned loop must keep
// its own off-grid endpoints (not snap to the grid) when a beat jump shifts
// it. Before the fix, shiftLiveBeatLoopRangeMs always returned the grid-exact
// beats/{beats[nextIn].t*1000, beats[nextOut].t*1000}, discarding this offset.
test('a beat jump shift preserves manually-set, non-grid-aligned loop endpoints', () => {
	// in_ms is 7ms after the nearest real beat (608ms); out_ms is 4ms before
	// the nearest real beat (2530ms) - neither sits exactly on the grid.
	assert.deepEqual(
		loops.shiftLiveBeatLoopRangeMs(DRIFTING_GRID, { in_ms: 615, out_ms: 2526 }, 2, 4000),
		{ in_ms: 1560, out_ms: 3536 },
		'the +7ms/-4ms manual offsets from the nearest grid beat must carry ' +
			'forward onto the shifted (+2 beat) endpoints, not collapse to the ' +
			'grid-exact 1553/3540'
	);
	// A loop that happens to already sit exactly on the grid is unaffected -
	// offset zero shifts to offset zero, matching the existing grid-aligned test.
	assert.deepEqual(
		loops.shiftLiveBeatLoopRangeMs(DRIFTING_GRID, { in_ms: 608, out_ms: 2530 }, 2, 4000),
		{ in_ms: 1553, out_ms: 3540 }
	);
});

// sol-review v1 P1 (BLOCKING) + P2 (non-blocking), pin 334a50710ef0: the beat
// INDEX bounds check alone cannot see a preserved manual offset. A negative
// in-offset on an early beat can push the shifted nextInMs below zero, and
// sufficiently different in/out offsets can make the shifted range empty or
// reversed - both while nextIn/nextOut are perfectly valid beat indices.
// Policy: refuse the whole shift (RangeError), matching this function's
// existing contract of refusing a boundary it cannot keep whole rather than
// silently clamping, reversing, or exiting.
test('a beat jump shift refuses a preserved offset that goes negative or reverses the range', () => {
	// in_ms is 140ms BEFORE its nearest real beat (608ms) - the DJ nudged the
	// loop-in early. Nearest-beat quantization still resolves to beat 608
	// (140ms is closer to 608 than to the prior beat at 135ms), so the offset
	// is preserved as -140ms. Shifting back 1 beat relocates that beat to the
	// very start of the grid (135ms), and 135 - 140 = -5: a negative endpoint.
	assert.throws(
		() => loops.shiftLiveBeatLoopRangeMs(DRIFTING_GRID, { in_ms: 468, out_ms: 2530 }, -1, 4000),
		/empty, reversed, or negative/i,
		'a preserved offset that pushes nextInMs below zero must be refused, not installed as a negative loop-in'
	);

	// A synthetic grid with alternating wide (1000ms) and narrow (50ms) gaps,
	// so an offset large enough to still be "nearest" its source beat becomes
	// larger than the narrow gap at the shifted destination. in_ms is 450ms
	// after beat 0 (nearest of [0, 1000]); out_ms is 450ms before beat 1000
	// (nearest of [0, 1000]). Shifting forward 1 beat lands in at 1000+450=1450
	// and out at 1050-450=600: out is now BEFORE in.
	const ALTERNATING_GRID = [
		{ n: 1, bpm: 120, t: 0 },
		{ n: 2, bpm: 120, t: 1.0 },
		{ n: 3, bpm: 120, t: 1.05 },
		{ n: 4, bpm: 120, t: 2.05 },
		{ n: 1, bpm: 120, t: 2.1 },
		{ n: 2, bpm: 120, t: 3.1 }
	];
	assert.throws(
		() => loops.shiftLiveBeatLoopRangeMs(ALTERNATING_GRID, { in_ms: 450, out_ms: 550 }, 1, 6000),
		/empty, reversed, or negative/i,
		'a shift landing on a narrower destination gap must be refused rather than returning a reversed range'
	);
});

test('resizing the saved engaged beat loop replaces its matching safety snapshot', () => {
	const before = { in_ms: 135, out_ms: 2040, engaged: true, beat_length: 4 };
	const after = { in_ms: 135, out_ms: 1080, engaged: true, beat_length: 2 };
	assert.deepEqual(
		loops.replaceMatchingSafetyLoopSnapshot(
			{ in_ms: 135, out_ms: 2040, beat_length: 4, armed: true },
			before,
			after
		),
		{ in_ms: 135, out_ms: 1080, beat_length: 2, armed: true },
		'an engaged loop resize must not leave an earlier safety-loop snapshot to restore later'
	);
	assert.deepEqual(
		loops.replaceMatchingSafetyLoopSnapshot(
			{ in_ms: 2040, out_ms: 4060, beat_length: 4, armed: true },
			before,
			after
		),
		{ in_ms: 2040, out_ms: 4060, beat_length: 4, armed: true },
		'a separately saved safety loop remains an explicit operator choice'
	);
});

const DRIFTING_SAFETY = { in_ms: 135, out_ms: 2040, beat_length: 4, armed: true };

test('phaseLockedSafetyLoop reconstructs a whole beat loop from its grid anchor', () => {
	const reentry = loops.phaseLockedSafetyLoop(DRIFTING_GRID, DRIFTING_SAFETY, 4000);
	assert.deepEqual(reentry, { in_ms: 135, out_ms: 2040, engaged: true, beat_length: 4 });
	assert.equal(
		loops.phaseLockedSafetyLoop(
			DRIFTING_GRID,
			{ in_ms: 135, out_ms: 2040, beat_length: null, armed: true },
			4000
		),
		null,
		'a safety loop without a beat count cannot be reconstructed on-grid'
	);
	assert.equal(
		loops.phaseLockedSafetyLoop([], { in_ms: 135, out_ms: 2040, beat_length: 4, armed: true }, 4000),
		null,
		'a gridless safety slot cannot be reconstructed on-grid'
	);
});

test('playbackReachedSafetyLoopOut fires when linear playhead crosses the saved out', () => {
	const base = {
		active: true,
		startPositionSec: 1.5,
		startContextTime: 0,
		tempoRatio: 1,
		safety: DRIFTING_SAFETY,
		liveLoop: null
	};
	assert.equal(
		loops.playbackReachedSafetyLoopOut({ ...base, atContextTime: 0.54 }),
		true,
		'linear playhead at the saved out crosses the threshold'
	);
	assert.equal(
		loops.playbackReachedSafetyLoopOut({
			...base,
			liveLoop: { in_ms: 135, out_ms: 2040, engaged: true, beat_length: 4 }
		}),
		false,
		'an already engaged live loop must not double-fire SAFE'
	);
	assert.equal(
		loops.playbackReachedSafetyLoopOut({
			...base,
			safety: { ...DRIFTING_SAFETY, armed: false },
			atContextTime: 0.54
		}),
		false,
		'a disarmed slot must not engage'
	);
	assert.equal(
		loops.playbackReachedSafetyLoopOut({
			...base,
			startPositionSec: 2.1,
			atContextTime: 20
		}),
		false,
		'a segment that started past the saved out did not reach it by playback'
	);
	assert.equal(
		loops.playbackReachedSafetyLoopOut({
			...base,
			startPositionSec: 0,
			atContextTime: 2.04
		}),
		true,
		'a first pass from before the in-point still reaches the saved out'
	);
	assert.equal(
		loops.playbackReachedSafetyLoopOut({ ...base, active: false, atContextTime: 0.54 }),
		false,
		'an inactive segment cannot trigger SAFE'
	);
});

test('disarmSafetyLoopOnExplicitExit keeps endpoints but clears armed', () => {
	assert.deepEqual(loops.disarmSafetyLoopOnExplicitExit(DRIFTING_SAFETY), {
		...DRIFTING_SAFETY,
		armed: false
	});
	assert.equal(loops.disarmSafetyLoopOnExplicitExit(null), null);
	assert.deepEqual(
		loops.disarmSafetyLoopOnExplicitExit({ ...DRIFTING_SAFETY, armed: false }),
		{ ...DRIFTING_SAFETY, armed: false }
	);
});

test('a shifted live loop keeps an exclusive-out beat jump inside the loop', () => {
	const shifted = { in_ms: 1553, out_ms: 3540 };
	assert.equal(
		loops.targetWithinShiftedLiveLoopMs(DRIFTING_GRID, 3540, shifted),
		3030,
		'an exclusive loop out is not a seekable position, so use the preceding real beat'
	);
	assert.equal(
		loops.targetWithinShiftedLiveLoopMs(DRIFTING_GRID, 3030, shifted),
		3030,
		'an already in-loop target must retain its exact PQTZ position'
	);
});

test('a jump past either grid end lands on the first or last real beat', () => {
	assertMs(math.beatJumpTargetMs(DRIFTING_GRID, 608, -8), 135);
	assertMs(math.beatJumpTargetMs(DRIFTING_GRID, 135, -4), 135);
	assertMs(math.beatJumpTargetMs(DRIFTING_GRID, 3540, 8), 4060);
	assertMs(math.beatJumpTargetMs(DRIFTING_GRID, 4060, 4), 4060);
});

test('beat jump rejects a zero, fractional, or non-finite beat count', () => {
	assert.throws(() => math.beatJumpTargetMs(DRIFTING_GRID, 608, 0), /non-zero integer/i);
	assert.throws(() => math.beatJumpTargetMs(DRIFTING_GRID, 608, 1.5), /non-zero integer/i);
	assert.throws(() => math.beatJumpTargetMs(DRIFTING_GRID, 608, Number.NaN), /non-zero integer/i);
});

test('beat jump refuses a missing or malformed beatgrid instead of guessing', () => {
	assert.throws(() => math.beatJumpTargetMs([], 608, 4), /at least 2 beats/i);
	assert.throws(() => math.beatJumpTargetMs(DRIFTING_GRID, -1, 4), /finite and >= 0/i);
	assert.throws(
		() =>
			math.beatJumpTargetMs(
				[
					{ n: 1, bpm: 127, t: 1 },
					{ n: 2, bpm: 127, t: 0.5 }
				],
				0,
				1
			),
		/strictly increasing/i
	);
});

test('beatJumpTargetWithinDurationMs clamps to the last real beat before decoded duration', () => {
	// The final grid beat (4060ms) sits past a decoded buffer that ends at
	// 4000ms - the same overshoot loopEndpointsWithinDurationMs documents for
	// loops. The clamped target must be an EXACT beat time (3540ms, index 7),
	// not the raw duration boundary, so a caller's own re-quantization snaps
	// it back to itself instead of re-selecting past the boundary.
	assert.equal(math.beatJumpTargetWithinDurationMs(DRIFTING_GRID, 4060, 4000), 3540);
	// A target already within duration passes through unchanged.
	assert.equal(math.beatJumpTargetWithinDurationMs(DRIFTING_GRID, 2040, 4000), 2040);
});

test('beatJumpTargetWithinDurationMs rejects a duration before every grid beat', () => {
	assert.throws(
		() => math.beatJumpTargetWithinDurationMs(DRIFTING_GRID, 4060, 100),
		/no PQTZ beat at or before decoded duration/i
	);
});

test('beatJumpMovesTransportWithinDuration catches a jump the engine would clamp to a no-op', () => {
	// From the last IN-DURATION beat (3540ms), the only grid movement forward
	// is to beat 4060ms, which sits past a 4000ms decoded duration. The engine
	// clamps that jump back to 3540ms - the same beat the deck is already on -
	// so this must report false even though the unclamped target reports a
	// real move for the exact same inputs (the bug this predicate exists to
	// catch).
	assert.equal(math.beatJumpTargetMs(DRIFTING_GRID, 3540, 1) === 3540, false);
	assert.equal(math.beatJumpMovesTransportWithinDuration(DRIFTING_GRID, 3540, 1, 4000), false);

	// A jump that stays within duration is unaffected by the clamp.
	assert.equal(math.beatJumpMovesTransportWithinDuration(DRIFTING_GRID, 2040, 1, 4000), true);
	// An unusable grid disables the control rather than throwing mid-render.
	assert.equal(math.beatJumpMovesTransportWithinDuration([], 0, 4, 4000), false);
});

// ------------------------------------------------------- dispatcher parity

test('beat_jump is a typed dispatcher command claiming the deck and sync scopes', () => {
	// Transport-moving, exactly like seek - it must serialize against sync.
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'beat_jump', deck: 1, beats: 4 }), [
		1,
		'sync'
	]);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'beat_jump', deck: 3, beats: -8 }), [
		3,
		'sync'
	]);
});

test('malformed beat_jump payloads are rejected before the engine is touched', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const ipcHandle = globalThis.window.musicDjToolsPerformance;
		await assert.rejects(
			() => ipcHandle.dispatch({ type: 'beat_jump', deck: 1, beats: 0 }),
			/non-zero integer/i
		);
		await assert.rejects(
			() => ipcHandle.dispatch({ type: 'beat_jump', deck: 1, beats: 2.5 }),
			/non-zero integer/i
		);
		await assert.rejects(
			() => ipcHandle.dispatch({ type: 'beat_jump', deck: 9, beats: 4 }),
			/deck must be one of/i
		);
		await assert.rejects(
			() => ipcHandle.dispatch({ type: 'beat_jump', deck: 1, beats: 4, start_ms: 10 }),
			/unexpected fields/i
		);
		// A well-formed command reaches the engine and fails there for the
		// honest reason: no track is loaded on a headless deck.
		await assert.rejects(
			() => ipcHandle.dispatch({ type: 'beat_jump', deck: 1, beats: 4 }),
			/no track loaded/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

// --------------------------------------------------- interval grid window

test('the interval grid defaults to readout mode on a 4/8/16/32 window', () => {
	// Readout mode stays the default so existing muscle memory is untouched.
	for (const deck of [1, 2, 3, 4]) {
		assert.equal(view.loopIntervalView[deck].gridMode, false);
		assert.equal(view.loopIntervalView[deck].gridBase, 4);
	}
	assert.deepEqual(view.loopIntervalChoices(4), [4, 8, 16, 32]);
});

test('the interval window offers base,2x,4x,8x and shifts by one power of two', () => {
	assert.deepEqual(view.loopIntervalChoices(8), [8, 16, 32, 64]);
	assert.equal(view.shiftedLoopIntervalBase(4, 1), 8);
	assert.equal(view.shiftedLoopIntervalBase(4, -1), 2);
});

test('the interval window stays inside the cluster MIN_BEATS / MAX_BEATS bounds', () => {
	assert.equal(view.LOOP_MIN_BEATS, 1);
	assert.equal(view.LOOP_MAX_BEATS, 512);
	// The window base leaves room for its own 8x, so 512 is reachable but
	// never exceeded: base tops out at 512 / 2**3 = 64.
	assert.equal(view.LOOP_MAX_GRID_BASE, 64);

	let base = 4;
	for (let i = 0; i < 10; i++) base = view.shiftedLoopIntervalBase(base, 1);
	assert.deepEqual(view.loopIntervalChoices(base), [64, 128, 256, 512]);
	for (let i = 0; i < 10; i++) base = view.shiftedLoopIntervalBase(base, -1);
	assert.deepEqual(view.loopIntervalChoices(base), [1, 2, 4, 8]);
});

test('an illegal interval window base is refused rather than rounded into range', () => {
	assert.throws(() => view.assertLoopGridBase(0), /integer in 1\.\.64/i);
	assert.throws(() => view.assertLoopGridBase(128), /integer in 1\.\.64/i);
	assert.throws(() => view.assertLoopGridBase(4.5), /integer in 1\.\.64/i);
	assert.throws(() => view.assertLoopGridBase(12), /power of two/i);
	assert.throws(() => view.setLoopIntervalBase(1, 12), /power of two/i);
});

// -------------------------------------- interval grid agent-native parity

test('every interval-grid view control has a matching typed command', () => {
	// View state only: nothing to serialize against the engine queue.
	assert.equal(
		ipc.performanceCommandQueueScopes({ type: 'loop_interval_mode', deck: 1, enabled: true }),
		null
	);
	assert.equal(
		ipc.performanceCommandQueueScopes({ type: 'loop_interval_base', deck: 1, base: 8 }),
		null
	);
});

test('an agent can drive AND read back the interval grid through the dispatcher', async () => {
	// Assertions read the state the dispatcher itself returns, not the
	// separately-loaded view module: loadTypeScriptModule bundles each entry
	// point standalone, so `view` above is a different instance from the copy
	// linked into `ipc`. Driving and observing through one handle is also the
	// contract an agent actually has.
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const ipcHandle = globalThis.window.musicDjToolsPerformance;
		assert.equal(ipcHandle.query().decks[2].loop_interval.grid_mode, false);

		const afterMode = await ipcHandle.dispatch({
			type: 'loop_interval_mode',
			deck: 2,
			enabled: true
		});
		assert.equal(afterMode.decks[2].loop_interval.grid_mode, true);
		// Sibling decks are untouched: the view state is per deck.
		assert.equal(afterMode.decks[1].loop_interval.grid_mode, false);

		const afterBase = await ipcHandle.dispatch({ type: 'loop_interval_base', deck: 2, base: 16 });
		assert.equal(afterBase.decks[2].loop_interval.grid_base, 16);
		assert.deepEqual(afterBase.decks[2].loop_interval.choices, [16, 32, 64, 128]);

		// Malformed payloads are refused before the view state moves.
		await assert.rejects(
			() => ipcHandle.dispatch({ type: 'loop_interval_base', deck: 2, base: 128 }),
			/integer in 1\.\.64/i
		);
		await assert.rejects(
			() => ipcHandle.dispatch({ type: 'loop_interval_mode', deck: 2, enabled: 'yes' }),
			/must be boolean/i
		);
		await assert.rejects(
			() => ipcHandle.dispatch({ type: 'loop_interval_base', deck: 2, base: 8, extra: 1 }),
			/unexpected fields/i
		);
		assert.equal(ipcHandle.query().decks[2].loop_interval.grid_base, 16);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

// --------------------------------------------------------- component wiring

test('beat jump reaches the engine only through the typed dispatcher', async () => {
	const deckSource = await readFile('src/lib/components/rb/Deck.svelte', 'utf8');
	const jumpSource = await readFile('src/lib/components/rb/deck/BeatJump.svelte', 'utf8');
	assert.match(
		deckSource,
		/runPerformanceCommandFromUi\(\{ type: 'beat_jump', deck: deckId, beats \}\)/
	);
	assert.match(deckSource, /<BeatJump\b/);
	assert.match(jumpSource, /data-performance-control="beat-jump"/);
	// The component owns no engine import - it only calls the injected prop.
	assert.doesNotMatch(jumpSource, /audio-engine/);
	assert.match(jumpSource, /onJump\(delta\)/);
	assert.match(jumpSource, /shiftLiveBeatLoopRangeMs\(beats, deck\.loop, delta, deck\.duration_ms\)/);
	assert.match(jumpSource, /live loop cannot shift/);
});

test('LOOP and JUMP are visible headings in their requested left-to-right columns', async () => {
	const deckSource = await readFile('src/lib/components/rb/Deck.svelte', 'utf8');
	const jumpSource = await readFile('src/lib/components/rb/deck/BeatJump.svelte', 'utf8');
	const loopSource = await readFile('src/lib/components/rb/deck/LoopCluster.svelte', 'utf8');
	// The controls remain out of cue-flex, with LOOP left and JUMP right.
	assert.match(
		deckSource,
		/<div class="loop-col">\s*<LoopCluster\b[\s\S]*?<BeatJump \{deck\} \{pending\} onJump=\{beatJump\} \/>/
	);
	const cueRegionStart = deckSource.indexOf('class="cue-flex"');
	const cueRegionEnd = deckSource.indexOf('class="loop-col"');
	assert.ok(cueRegionStart >= 0 && cueRegionEnd > cueRegionStart, 'cue-flex region is present');
	assert.doesNotMatch(deckSource.slice(cueRegionStart, cueRegionEnd), /<BeatJump\b/);
	assert.match(
		deckSource,
		/\.loop-col \{[\s\S]*?display: flex;[\s\S]*?flex-direction: row;[\s\S]*?align-items: flex-start;/
	);
	assert.match(loopSource, /<span class="column-label">LOOP<\/span>/);
	assert.match(jumpSource, /<span class="column-label">JUMP<\/span>/);
	assert.match(jumpSource, /grid-template-columns: repeat\(2, minmax\(18px, 1fr\)\)/);
	assert.match(jumpSource, /width: 38px;/);
});

test('every beat jump control offers the four -8 -4 +4 +8 steps', async () => {
	const jumpSource = await readFile('src/lib/components/rb/deck/BeatJump.svelte', 'utf8');
	assert.match(jumpSource, /JUMPS: readonly number\[\] = \[-8, -4, 4, 8\]/);
	assert.match(jumpSource, /\{#each JUMPS as delta \(delta\)\}/);
	assert.match(jumpSource, /data-beats=\{delta\}/);
	assert.match(jumpSource, /\{_label\(delta\)\}/);
});

test('an inert beat jump explains itself rather than silently doing nothing', async () => {
	const jumpSource = await readFile('src/lib/components/rb/deck/BeatJump.svelte', 'utf8');
	assert.match(jumpSource, /no track loaded/);
	assert.match(jumpSource, /track has no usable beatgrid - beat jump unavailable/);
	assert.match(jumpSource, /beatgrid beats remain ahead/);
	assert.match(jumpSource, /title=\{_title\(delta\)\}/);
});

test('the loop interval grid is an opt-in mode; the single readout stays default', async () => {
	const loopSource = await readFile('src/lib/components/rb/deck/LoopCluster.svelte', 'utf8');
	// The cluster reads the mode from the shared view state, so the same
	// command drives it from a click and from an agent.
	assert.match(loopSource, /loopIntervalView\[deckId\]\.gridMode/);
	assert.match(loopSource, /data-performance-control="loop-interval-mode"/);
	// Readout mode keeps the original engage/disengage control.
	assert.match(loopSource, /data-performance-control="loop"/);
});

test('the interval grid renders both window shifts and the four choices', async () => {
	const loopSource = await readFile('src/lib/components/rb/deck/LoopCluster.svelte', 'utf8');
	assert.match(loopSource, /data-performance-control="loop-interval-down"/);
	assert.match(loopSource, /data-performance-control="loop-interval-up"/);
	assert.match(loopSource, /data-performance-control="loop-interval"/);
	assert.match(loopSource, /loopIntervalChoices\(gridBase\)/);
});

test('the interval grid mutates view state only through the typed commands', async () => {
	const loopSource = await readFile('src/lib/components/rb/deck/LoopCluster.svelte', 'utf8');
	const deckSource = await readFile('src/lib/components/rb/Deck.svelte', 'utf8');
	// The cluster dispatches; it never writes loopIntervalView directly.
	assert.match(loopSource, /await onIntervalMode\(!intervalGrid\)/);
	assert.match(loopSource, /await onIntervalBase\(next\)/);
	assert.doesNotMatch(loopSource, /loopIntervalView\[deckId\]\.\w+\s*=/);
	assert.match(
		deckSource,
		/runPerformanceCommandFromUi\(\{ type: 'loop_interval_mode', deck: deckId, enabled \}\)/
	);
	assert.match(
		deckSource,
		/runPerformanceCommandFromUi\(\{ type: 'loop_interval_base', deck: deckId, base \}\)/
	);
});

test('picking an interval engages a loop through the same command path as the readout', async () => {
	const loopSource = await readFile('src/lib/components/rb/deck/LoopCluster.svelte', 'utf8');
	assert.match(loopSource, /async function chooseInterval/);
	assert.match(loopSource, /await onEngage\(n\)/);
	// Re-picking the live length exits, matching the readout toggle. Reads
	// engagedIntervalLength (the engine's own deck.loop.beat_length), not the
	// local beatLength mirror: a manually drawn loop reports beat_length null,
	// so it must never read as "already at n" and get exited by mistake.
	assert.match(loopSource, /if \(engagedIntervalLength === n\) \{\s*await onDisengage\(\)/);
	assert.doesNotMatch(loopSource, /engaged && beatLength === /);
	// The engaged length is visually marked in the grid.
	assert.match(loopSource, /class:selected=\{engagedIntervalLength === choice\}/);
});

test('a clipped engaged loop stays choosable so it can be exited from the grid', async () => {
	const loopSource = await readFile('src/lib/components/rb/deck/LoopCluster.svelte', 'utf8');
	// _canChoose delegates to canChooseInterval (loop-cluster-actions.ts, a
	// distinct pure-logic concern split out of the component).
	assert.match(
		loopSource,
		/function _canChoose\(n: number\): boolean \{\s*return canChooseInterval\(/
	);
	const actionsSource = await readFile(
		'src/lib/components/rb/deck/loop-cluster-actions.ts',
		'utf8'
	);
	// An agent-created loop can report a beat_length the grid's own fit math
	// would refuse (the engine clips the endpoint to duration rather than
	// rejecting it). canChooseInterval must short-circuit true for that
	// engaged length BEFORE the beatLoopFitsWithinDuration fit check, or the
	// choice renders selected-but-disabled and chooseInterval's disengage
	// branch (asserted above) becomes unreachable.
	assert.match(
		actionsSource,
		/function canChooseInterval\([\s\S]*?\): boolean \{[\s\S]*?if \(engagedIntervalLength === n\) return true;[\s\S]*?beatLoopFitsWithinDuration/
	);
});

// -------------------------------------- PIN f11c66 / 02978b regression math

test('if a live-loop beat jump clears or changes loop length then the pin is broken', () => {
	// 4-beat loop: beats[1]=608 .. beats[5]=2530.
	const loop = { in_ms: 608, out_ms: 2530 };
	const beatIndexOf = (ms) => DRIFTING_GRID.findIndex((beat) => Math.abs(beat.t * 1000 - ms) < 1e-6);
	const originalBeatLength = beatIndexOf(loop.out_ms) - beatIndexOf(loop.in_ms);
	assert.equal(originalBeatLength, 4);

	// Mirrors AudioEngineController.beatJump's own sequence: shift the live
	// loop by the jumped beats first, then land the target inside it.
	const shifted = loops.shiftLiveBeatLoopRangeMs(DRIFTING_GRID, loop, 1, 5000);
	const shiftedBeatLength = beatIndexOf(shifted.out_ms) - beatIndexOf(shifted.in_ms);
	assert.equal(shiftedBeatLength, originalBeatLength, 'beat jump must never change the loop length');

	const rawTarget = math.beatJumpTargetMs(DRIFTING_GRID, 2040, 1);
	const targetMs = math.beatJumpTargetWithinDurationMs(DRIFTING_GRID, rawTarget, 5000);
	const landedMs = loops.targetWithinShiftedLiveLoopMs(DRIFTING_GRID, targetMs, shifted);
	assert.ok(
		landedMs >= shifted.in_ms && landedMs < shifted.out_ms,
		'the jump must land the playhead inside the shifted loop, never exit it'
	);
});

test('if a fresh four-beat loop starts on the upcoming beat instead of the preceding beat one then the pin is broken', () => {
	// 1.0s sits between beat index 2 (1.08 is actually AHEAD; nearest real
	// beat below 1.0s is index 1 at 0.608) and the next downbeat (n===1) at
	// 2.04s. A fresh 4-beat loop with no explicit start_ms must anchor on the
	// PRECEDING downbeat (0.135s, the only earlier n===1 beat), never the
	// upcoming one (2.04s) and never the plain nearest-beat quantization.
	const anchorMs = loops.precedingDownbeatMs(DRIFTING_GRID, 1000);
	assertMs(anchorMs, 135);
	assert.notEqual(anchorMs, math.quantizeToNearestBeat(DRIFTING_GRID, 1) * 1000);

	const range = loops.exactBeatLoopRangeMs(DRIFTING_GRID, 1000, 4, anchorMs);
	assertMs(range.in_ms, 135);
	assertMs(range.out_ms, 2040);
});

test('precedingDownbeatMs picks the latest bar-1 beat at or before the position, never ahead of it', () => {
	assertMs(loops.precedingDownbeatMs(DRIFTING_GRID, 2040), 2040); // exactly on a downbeat
	assertMs(loops.precedingDownbeatMs(DRIFTING_GRID, 2100), 2040); // just past it
	assertMs(loops.precedingDownbeatMs(DRIFTING_GRID, 4060), 4060); // last downbeat in the grid
});

test('precedingDownbeatMs refuses a position before every downbeat or a broken grid', () => {
	assert.throws(() => loops.precedingDownbeatMs(DRIFTING_GRID, 50), /no PQTZ downbeat/i);
	assert.throws(() => loops.precedingDownbeatMs([], 1000), /at least 2 beats/i);
	assert.throws(() => loops.precedingDownbeatMs(DRIFTING_GRID, -1), /finite and non-negative/i);
});

test('resizedLoopRangeMs keeps loop-in fixed for a start anchor', () => {
	const loop = { in_ms: 608, out_ms: 2530 }; // beats 1..5, 4 beats
	const range = loops.resizedLoopRangeMs(DRIFTING_GRID, loop, 2, 'start', 5000);
	assertMs(range.in_ms, 608);
	assertMs(range.out_ms, 1553); // beats[3]
});

test('resizedLoopRangeMs keeps loop-out fixed for an end anchor', () => {
	const loop = { in_ms: 608, out_ms: 2530 }; // beats 1..5, 4 beats
	const range = loops.resizedLoopRangeMs(DRIFTING_GRID, loop, 2, 'end', 5000);
	assertMs(range.in_ms, 1553); // beats[3]
	assertMs(range.out_ms, 2530);
});

test('resizedLoopRangeMs splits a center anchor evenly on both sides, like a centered transform', () => {
	// beats 3..5 (1553..2530), 2 beats long. Doubling to 4 beats adds 1 beat
	// on each side: beats 2..6 (1080..3030).
	const loop = { in_ms: 1553, out_ms: 2530 };
	const grown = loops.resizedLoopRangeMs(DRIFTING_GRID, loop, 4, 'center', 5000);
	assertMs(grown.in_ms, 1080);
	assertMs(grown.out_ms, 3030);

	// beats 0..8 (135..4060), 8 beats long. Halving to 4 beats removes 2
	// beats on each side: beats 2..6 (1080..3030).
	const wide = { in_ms: 135, out_ms: 4060 };
	const shrunk = loops.resizedLoopRangeMs(DRIFTING_GRID, wide, 4, 'center', 5000);
	assertMs(shrunk.in_ms, 1080);
	assertMs(shrunk.out_ms, 3030);
});

test('resizedLoopRangeMs refuses a center resize that cannot split evenly across both sides', () => {
	const loop = { in_ms: 608, out_ms: 2040 }; // beats 1..4, 3 beats
	assert.throws(
		() => loops.resizedLoopRangeMs(DRIFTING_GRID, loop, 4, 'center', 5000),
		/not evenly splittable/i
	);
});

test('resizedLoopRangeMs throws rather than clipping when a resize does not fit the grid or duration', () => {
	const loop = { in_ms: 608, out_ms: 2530 }; // beats 1..5
	// 'end' anchored resize asking for more beats than exist before loop-out.
	assert.throws(
		() => loops.resizedLoopRangeMs(DRIFTING_GRID, loop, 8, 'end', 5000),
		/does not fit/i
	);
	// A resize that would fit the grid but overruns a short decoded duration.
	assert.throws(
		() => loops.resizedLoopRangeMs(DRIFTING_GRID, loop, 6, 'start', 3000),
		/exceeds decoded duration/i
	);
	// Malformed inputs are refused explicitly.
	assert.throws(
		() => loops.resizedLoopRangeMs(DRIFTING_GRID, loop, 0, 'start', 5000),
		/positive integer/i
	);
	assert.throws(
		() => loops.resizedLoopRangeMs(DRIFTING_GRID, { in_ms: 2530, out_ms: 608 }, 2, 'start', 5000),
		/finite 0 <= in_ms < out_ms/i
	);
});

// -------------------------------------------------- SAFE loop-out wiring

test('SAFE engages at loop out via playbackReachedSafetyLoopOut, not at natural end', async () => {
	const engineSource = await readFile('src/lib/rb/audio-engine.svelte.ts', 'utf8');
	const naturalEndBlock = engineSource.match(
		/if \(naturalEndNeedsRevisionedStop[\s\S]*?\n\t\}/
	)?.[0];
	assert.ok(naturalEndBlock, 'natural-end block is present');
	assert.doesNotMatch(naturalEndBlock, /phaseLockedSafetyLoop/);
	assert.match(engineSource, /playbackReachedSafetyLoopOut\(/);
	assert.match(engineSource, /st\.safety_loop = disarmSafetyLoopOnExplicitExit\(st\.safety_loop\)/);
	const seekExitBlock = engineSource.match(/if \(exitLoop\) st\.loop = null;[\s\S]{0,200}/)?.[0];
	assert.ok(seekExitBlock, 'seek-out loop exit is present');
	assert.doesNotMatch(seekExitBlock, /disarmSafetyLoopOnExplicitExit/);
});

// -------------------------------------------------- restart-loop wiring

test('engageBeatLoop anchors a fresh four-beat loop on the preceding downbeat, no other length', async () => {
	const engineSource = await readFile('src/lib/rb/audio-engine.svelte.ts', 'utf8');
	assert.match(
		engineSource,
		/beats === 4 && startMs === undefined \? precedingDownbeatMs\(grid, currentMs\) : startMs/
	);
});

test('engageBeatLoop restarts (playhead back to loop-in, loop retained) on an exact re-issue', async () => {
	const engineSource = await readFile('src/lib/rb/audio-engine.svelte.ts', 'utf8');
	assert.match(
		engineSource,
		/st\.loop\.beat_length === beats &&\s*st\.loop\.in_ms === range\.in_ms &&\s*st\.loop\.out_ms === range\.out_ms/
	);
	// The restart branch must not fall through to setLoop's ordinary
	// leave-the-playhead-running behaviour.
	assert.match(engineSource, /if \(isRestart\) \{/);
	assert.match(engineSource, /_setPausedPosition\(deck, range\.in_ms\)/);
});

test('the LoopCluster restart control is a small no-text glyph button beside the readout', async () => {
	const loopSource = await readFile('src/lib/components/rb/deck/LoopCluster.svelte', 'utf8');
	assert.match(loopSource, /data-performance-control="loop-restart"/);
	assert.match(loopSource, /aria-label="restart loop"/);
	// No visible text label - glyph only.
	const buttonMatch = loopSource.match(
		/<button\s+class="restart-btn"[\s\S]*?<\/button>/
	);
	assert.ok(buttonMatch, 'restart button markup is present');
	assert.doesNotMatch(buttonMatch[0], />[A-Za-z]{2,}</, 'the restart control must carry no text label');
	// The restart command itself (beats/startMs derivation from the live
	// loop) moved to restartEngageArgs (loop-cluster-actions.ts); the
	// component just forwards its result.
	assert.match(loopSource, /const args = restartEngageArgs\(deck\)/);
	assert.match(loopSource, /await onEngage\(args\.beats, args\.startMs\)/);
	const actionsSource = await readFile(
		'src/lib/components/rb/deck/loop-cluster-actions.ts',
		'utf8'
	);
	assert.match(
		actionsSource,
		/return \{ beats: deck\.loop\.beat_length, startMs: deck\.loop\.in_ms \}/
	);
});

test('shift/opt click on loop halve or double resize with an end or center anchor, plain click keeps start', async () => {
	const loopSource = await readFile('src/lib/components/rb/deck/LoopCluster.svelte', 'utf8');
	// The modifier-key anchor rule and the resize command construction moved
	// to resizeAnchorOf / reapplyEngageArgs (loop-cluster-actions.ts); the
	// component still calls through _resizeAnchor and wires the same clicks.
	assert.match(loopSource, /function _resizeAnchor\(event: MouseEvent\)/);
	assert.match(loopSource, /return resizeAnchorOf\(event\)/);
	assert.match(loopSource, /onclick=\{\(event\) => void halve\(event\)\}/);
	assert.match(loopSource, /onclick=\{\(event\) => void double\(event\)\}/);
	const actionsSource = await readFile(
		'src/lib/components/rb/deck/loop-cluster-actions.ts',
		'utf8'
	);
	assert.match(actionsSource, /if \(event\.altKey\) return 'center';/);
	assert.match(actionsSource, /if \(event\.shiftKey\) return 'end';/);
	assert.match(
		actionsSource,
		/resizedLoopRangeMs\(gridBeats, deck\.loop, beatLength, anchor, deck\.duration_ms\)/
	);
});

// ------------------------------------------------------- grid quantize (8)

// No n===1 beat at all - a track whose downbeat detector found nothing.
const NO_DOWNBEAT_GRID = [
	{ n: 2, bpm: 128, t: 0.1 },
	{ n: 3, bpm: 128, t: 0.57 },
	{ n: 4, bpm: 128, t: 1.04 }
];

// Exactly one n===1 beat, valid 1,2,3,4 cadence throughout.
const ONE_DOWNBEAT_GRID = [
	{ n: 3, bpm: 128, t: 0.1 },
	{ n: 4, bpm: 128, t: 0.57 },
	{ n: 1, bpm: 128, t: 1.04 },
	{ n: 2, bpm: 128, t: 1.51 }
];

// Four full bars, valid cadence, so downbeats sit at real bar boundaries
// 0, 1, 2, 3 (by array index AND by musical position - the schema's
// unbroken n=1,2,3,4 cadence makes the two equivalent, since a bar cannot
// go missing mid-grid without breaking `validateBeatGrid`).
const FOUR_BAR_GRID = [
	{ n: 1, bpm: 128, t: 0.0 },
	{ n: 2, bpm: 128, t: 0.47 },
	{ n: 3, bpm: 128, t: 0.94 },
	{ n: 4, bpm: 128, t: 1.41 },
	{ n: 1, bpm: 128, t: 1.88 }, // bar 1
	{ n: 2, bpm: 128, t: 2.35 },
	{ n: 3, bpm: 128, t: 2.82 },
	{ n: 4, bpm: 128, t: 3.29 },
	{ n: 1, bpm: 128, t: 3.76 }, // bar 2
	{ n: 2, bpm: 128, t: 4.23 },
	{ n: 3, bpm: 128, t: 4.7 },
	{ n: 4, bpm: 128, t: 5.17 },
	{ n: 1, bpm: 128, t: 5.64 } // bar 3
];

test('quantizeToNearestGridBeat(8) degrades to the nearest plain beat when no downbeat exists', () => {
	assert.equal(
		math.quantizeToNearestGridBeat(NO_DOWNBEAT_GRID, 0.6, 8),
		math.quantizeToNearestBeat(NO_DOWNBEAT_GRID, 0.6),
		'no downbeat at all must still return a real grid line, not throw'
	);
	assert.equal(math.quantizeToNearestGridBeat(NO_DOWNBEAT_GRID, 0.6, 4), 0.57);
});

test('quantizeToNearestGridBeat(8) degrades to the single downbeat when only one exists', () => {
	assert.equal(math.quantizeToNearestGridBeat(ONE_DOWNBEAT_GRID, 1.5, 8), 1.04);
	assert.equal(math.quantizeToNearestGridBeat(ONE_DOWNBEAT_GRID, 1.5, 4), 1.04);
});

test('quantizeToNearestGridBeat(8) selects every other real bar downbeat (0, 2, ...), never an odd one', () => {
	// twoBarBeats here are bars 0 (0.0s) and 2 (3.76s); bars 1 (1.88s) and 3
	// (5.64s) must never be returned for gridBeats:8.
	assert.equal(math.quantizeToNearestGridBeat(FOUR_BAR_GRID, 0.5, 8), 0.0);
	assert.equal(math.quantizeToNearestGridBeat(FOUR_BAR_GRID, 2.9, 8), 3.76);
	assert.equal(math.quantizeToNearestGridBeat(FOUR_BAR_GRID, 5.5, 8), 3.76);
	// gridBeats:4 has no such restriction - bar 1 and bar 3 are reachable.
	assert.equal(math.quantizeToNearestGridBeat(FOUR_BAR_GRID, 1.9, 4), 1.88);
});
