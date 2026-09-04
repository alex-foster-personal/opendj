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
	const cueRegionStart = deckSource.indexOf('<div class="cue-flex">');
	const cueRegionEnd = deckSource.indexOf('\n\t\t</div>', cueRegionStart);
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
	// An agent-created loop can report a beat_length the grid's own fit math
	// would refuse (the engine clips the endpoint to duration rather than
	// rejecting it). _canChoose must short-circuit true for that engaged
	// length BEFORE the beatLoopFitsWithinDuration fit check, or the choice
	// renders selected-but-disabled and chooseInterval's disengage branch
	// (asserted above) becomes unreachable.
	assert.match(
		loopSource,
		/function _canChoose\(n: number\): boolean \{[^}]*if \(engagedIntervalLength === n\) return true;[^}]*beatLoopFitsWithinDuration/s
	);
});
