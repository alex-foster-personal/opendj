/**
 * ADVERSARIAL (round 1; was RED, FIXED by beatJumpSeekPlan): a beat jump on a
 * PLAYING deck is not phase-preserving. It moves the playhead by a
 * non-integer number of beats, so the deck's own rhythm skips and every
 * Beat Sync follower of a jumping master is knocked off phase.
 *
 * Engine path (src/lib/rb/audio-engine.svelte.ts `beatJump`, ~line 3782):
 *   anchorMs = live position projected to the next schedule time
 *   target   = beatJumpTargetMs(grid, anchorMs, n)    -> an exact beat .t
 *   quantizedSeek(deck, target)                        -> fires at that time
 * `beatJumpTargetMs` (src/lib/rb/beat-sync-math.ts:523) first snaps the
 * anchor to its NEAREST beat and drops the fraction, and the seek is not
 * deferred to a beat boundary, so the deck jumps from phase p of its beat to
 * phase 0 of the target beat: a jump of n - p beats. At 128 BPM and p = 0.3
 * that is a 141 ms hiccup in the deck's own groove.
 *
 * With BeatSyncMax off nothing re-anchors the followers of a master that does
 * this (`planSeekSync` returns 'free'), so they are now p beats off. The
 * phase lock then either re-seeks them (p > 0.25: a second audible jump) or
 * trims for tens of seconds (p < 0.25, see
 * beat-sync-adversarial-phase-lock-recovery.test.mjs).
 *
 * The existing deck-loop-beatjump.test.mjs pins the static contract (a
 * between-beats anchor snaps to its nearest beat); that is right for a
 * PAUSED deck and is not what this file disputes.
 *
 * There are TWO phase-erasers on this path, and fixing one is not enough
 * (verified by mutation): `beatJumpTargetMs` drops the fraction, AND
 * `quantizedSeek` (skipGridQuantize=false when no loop is engaged) re-snaps
 * whatever target it is given to the nearest grid line via
 * `quantizedSeekDecisionMs` (src/lib/player/transport/loops.ts:340). The same
 * second eraser hits every quantized seek on a playing deck: waveform click,
 * CUE jump, immediate hot cue.
 *
 * Fix direction: for a playing deck either carry the fractional phase
 * (target + p * interval at the target beat) AND pass skipGridQuantize=true
 * from beatJump, or arm the jump at the deck's next beat boundary, the way
 * BeatSyncMax hot cues already arm at the next downbeat
 * (`planHotCueTrigger`).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let bsm;
let loops;
let pl;

before(async () => {
	bsm = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
	loops = await loadTypeScriptModule('src/lib/player/transport/loops.ts');
	pl = await loadTypeScriptModule('src/lib/rb/phase-lock.ts');
});

function grid(bpm, startSec, count) {
	const spb = 60 / bpm;
	return Array.from({ length: count }, (_, i) => ({ n: (i % 4) + 1, bpm, t: startSec + i * spb }));
}

const GRID = grid(128, 0.1, 600);
const SPB = 60 / 128;
const DURATION_MS = GRID.at(-1).t * 1000;

/**
 * The engine's composed beat-jump landing for a deck at `anchorMs`: the plan
 * `beatJump` takes (`beatJumpSeekPlan`, pinned to the engine by the SOURCE
 * test below), then `quantizedSeek`'s own target decision with quantize on and
 * no loop (the common case).
 */
function engineBeatJumpLandingMs(anchorMs, beats, quantizeGridBeats, playing = true, grid = GRID) {
	const plan = bsm.beatJumpSeekPlan(grid, anchorMs, beats, grid.at(-1).t * 1000, playing);
	return loops.quantizedSeekDecisionMs(grid, plan.targetMs, quantizeGridBeats, plan.skipGridQuantize, null)
		.targetMs;
}

function movedBeats(grid, fromMs, toMs) {
	return pl.gridBeatPosition(grid, toMs / 1000) - pl.gridBeatPosition(grid, fromMs / 1000);
}

for (const [phase, jump] of [[0.1, 4], [0.3, 4], [0.45, 4], [0.3, -4], [0.7, 1], [0.3, -1]]) {
	for (const quantizeGridBeats of [1, 4, 8]) {
		test(`playing deck at phase ${phase}: a ${jump}-beat jump moves exactly ${jump} beats (quantize ${quantizeGridBeats})`, () => {
			const anchorMs = (GRID[200].t + phase * SPB) * 1000;
			const moved = movedBeats(GRID, anchorMs, engineBeatJumpLandingMs(anchorMs, jump, quantizeGridBeats));
			// Observed on the unfixed code: moved = jump - phase (e.g. 3.7 for +4
			// at phase 0.3), i.e. a 141 ms rhythm skip at 128 BPM.
			assert.ok(
				Math.abs(moved - jump) < 1e-6,
				`moved ${moved.toFixed(3)} beats: the groove skips ` +
					`${(Math.abs(moved - jump) * SPB * 1000).toFixed(0)} ms`
			);
		});
	}
}

test('playing deck on a drifting grid: the phase is carried through the TARGET beat\'s own interval', () => {
	// Beat 10 is 500 ms long, beat 14 is 520 ms: phase 0.25 lands 130 ms in.
	const drifting = Array.from({ length: 40 }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: i < 12 ? i * 0.5 : 6 + (i - 12) * 0.52 }));
	const anchorMs = (drifting[10].t + 0.125) * 1000;
	const landing = engineBeatJumpLandingMs(anchorMs, 4, 1, true, drifting);
	assert.ok(Math.abs(landing - (drifting[14].t + 0.13) * 1000) < 1e-6, `landed at ${landing}`);
});

test('control: a jump fired exactly on a beat is already phase-exact, playing or paused', () => {
	const anchorMs = GRID[200].t * 1000;
	for (const playing of [true, false]) {
		assert.ok(Math.abs(engineBeatJumpLandingMs(anchorMs, 4, 1, playing) - GRID[204].t * 1000) < 1e-6);
	}
});

test('control: a PAUSED deck still snaps to the nearest beat and re-quantizes (unchanged)', () => {
	const anchorMs = (GRID[200].t + 0.3 * SPB) * 1000;
	assert.equal(engineBeatJumpLandingMs(anchorMs, 4, 1, false), GRID[204].t * 1000);
	assert.equal(engineBeatJumpLandingMs((GRID[200].t + 0.7 * SPB) * 1000, 4, 1, false), GRID[205].t * 1000);
	// The deck's own 4-beat quantize still applies to a paused jump.
	const plan = bsm.beatJumpSeekPlan(GRID, anchorMs, 1, DURATION_MS, false);
	assert.equal(plan.skipGridQuantize, false);
	assert.equal(
		engineBeatJumpLandingMs(anchorMs, 1, 4, false),
		loops.quantizedSeekDecisionMs(GRID, GRID[201].t * 1000, 4, false, null).targetMs
	);
});

test('control: a playing jump past either grid end still stops on the first or last beat', () => {
	const nearEnd = (GRID.at(-3).t + 0.4 * SPB) * 1000;
	assert.equal(engineBeatJumpLandingMs(nearEnd, 8, 1), GRID.at(-1).t * 1000);
	const nearStart = (GRID[2].t + 0.4 * SPB) * 1000;
	assert.equal(engineBeatJumpLandingMs(nearStart, -8, 1), GRID[0].t * 1000);
	// Before the first beat (an intro) there is no phase to keep: it snaps.
	assert.equal(engineBeatJumpLandingMs(50, 4, 1), GRID[4].t * 1000);
});

test('control: a playing jump from exactly ON a beat to exactly the LAST beat lands there', () => {
	// A whole target index reads no beat past the last one, and the last beat
	// itself is off the grid's interior (no interval after it to measure in).
	const last = GRID.length - 1;
	assert.equal(bsm.beatTimeAt(GRID, last), GRID[last].t);
	assert.equal(bsm.gridBeatPosition(GRID, GRID[last].t), null);
	assert.equal(bsm.gridBeatPosition(GRID, GRID[last - 1].t), last - 1);
	assert.equal(engineBeatJumpLandingMs(GRID[last - 4].t * 1000, 4, 1), GRID[last].t * 1000);
	assert.equal(engineBeatJumpLandingMs(GRID[last].t * 1000, -4, 1), GRID[last - 4].t * 1000);
});

test('control: a playing jump inside an engaged loop keeps its phase and stays in the shifted loop', () => {
	const loop = { in_ms: GRID[200].t * 1000, out_ms: GRID[208].t * 1000 };
	const shifted = loops.shiftLiveBeatLoopRangeMs(GRID, { ...loop, engaged: true, beat_length: 8 }, 4, DURATION_MS);
	const anchorMs = (GRID[203].t + 0.3 * SPB) * 1000;
	const plan = bsm.beatJumpSeekPlan(GRID, anchorMs, 4, DURATION_MS, true);
	const landing = loops.targetWithinShiftedLiveLoopMs(GRID, plan.targetMs, shifted);
	assert.ok(Math.abs(movedBeats(GRID, anchorMs, landing) - 4) < 1e-6);
});

test('control: every OTHER quantized seek still snaps to the grid (cue, hot cue, waveform, IPC, Rust)', () => {
	const between = (GRID[200].t + 0.3 * SPB) * 1000;
	assert.equal(loops.quantizedSeekDecisionMs(GRID, between, 1, false, null).targetMs, GRID[200].t * 1000);
	const engine = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	assert.match(engine, /async cueJump\(deck: DeckId, ms: number\): Promise<void> \{\n\t\tawait this\.quantizedSeek\(deck, ms\);/);
	assert.match(engine, /await this\.quantizedSeek\(deck, st\.cue_ms, undefined, pressT0Ms\);/);
	const ipc = readFrontendSource('src/lib/rb/performance-ipc.svelte.ts');
	assert.match(ipc, /jump: \(deck, positionMs, pressT0Ms\) => engine\.quantizedSeek\(deck, positionMs, undefined, pressT0Ms\)/);
	assert.match(ipc, /await engine\.quantizedSeek\(command\.deck, command\.position_ms\);/);
	const rust = readFrontendSource('src/lib/audio-engine/rust-sync.ts');
	assert.match(rust, /quantizedSeekDecisionMs\(beats, ms, beats === null \? 1 : _gridBeats\(st\), false, st\.loop\)/);
});

test('SOURCE: beatJump takes beatJumpSeekPlan with the deck\'s desired transport and passes its skip flag', () => {
	const engine = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const start = engine.indexOf('async beatJump(deck: DeckId, beats: number): Promise<void> {');
	assert.ok(start > 0, 'beatJump not found');
	const body = engine.slice(start, engine.indexOf('\n\t}\n', start));
	assert.match(
		body,
		/const \{ targetMs, skipGridQuantize \} = beatJumpSeekPlan\(grid, anchorMs, beats, _durationSec\(deck\) \* 1000, _rt\[deck\]\.desiredActive\);/
	);
	// Round 2: the jump also hands its beat count through, so a playing deck lands
	// that many beats from wherever it is when the schedule lands (`phaseKeepingLandingSec`, beat-sync-math.ts).
	assert.match(
		body,
		/await this\.quantizedSeek\(deck, targetMs, skipGridQuantize, undefined, beats\);$/
	);
});
