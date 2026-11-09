import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * CUES-9967 - rekordbox / CDJ ACTIVE LOOP (mimicked): one stored memory loop
 * per track is flagged active; loading the track arms it in the deck's safety
 * slot, and playback reaching it engages it through the safety-loop path.
 *
 * Regression lines:
 * - if activeLoopSafetySlot does not return the active memory loop armed with beat_length null then broken
 * - if a track with no active loop does not leave the safety slot untouched then broken
 * - if an active hot-cue loop or a loop past the end arms the slot then broken
 * - if a stored (null-beat) safety slot does not engage exactly as saved then broken
 * - if a grid safety slot stops engaging phase-locked then broken
 * - if an explicit exit clears the active slot's endpoints instead of only disarming then broken
 * - if storedLoops does not put the active memory row first and mark other memory rows Set-active eligible then broken
 */

let loops;
let loopSource;

function cue(over) {
	return {
		kind: 'loop',
		slot: null,
		in_ms: 1000,
		out_ms: 3000,
		is_loop: true,
		active_loop: false,
		beat_loop_size: null,
		color_table_index: null,
		comment: null,
		...over
	};
}

const GRID = [0, 500, 1000, 1500, 2000, 2500, 3000, 3500].map((ms, i) => ({ n: (i % 4) + 1, bpm: 120, t: ms / 1000 }));

before(async () => {
	loops = await loadTypeScriptModule('src/lib/player/transport/loops.ts');
	loopSource = await loadTypeScriptModule('src/lib/rb/loop-source.ts');
});

test('the active memory loop becomes an armed stored safety slot', () => {
	const cues = [cue({ in_ms: 500, out_ms: 900 }), cue({ in_ms: 5000, out_ms: 7000, active_loop: true })];
	assert.deepEqual(loops.activeLoopSafetySlot(cues, 60_000), {
		in_ms: 5000,
		out_ms: 7000,
		beat_length: null,
		armed: true
	});
});

test('a track with no active loop leaves the slot untouched', () => {
	assert.equal(loops.activeLoopSafetySlot([cue({})], 60_000), null);
	assert.equal(loops.activeLoopSafetySlot(null, 60_000), null);
});

test('an active hot-cue loop or a loop past the end never arms the slot', () => {
	assert.equal(loops.activeLoopSafetySlot([cue({ kind: 'hot_cue', slot: 'A', active_loop: true })], 60_000), null);
	assert.equal(loops.activeLoopSafetySlot([cue({ active_loop: true, out_ms: 70_000 })], 60_000), null);
});

test('a stored safety slot engages exactly as saved', () => {
	const slot = { in_ms: 1000, out_ms: 3000, beat_length: null, armed: true };
	assert.deepEqual(loops.safetyLoopToEngage([], slot, 60_000), {
		in_ms: 1000,
		out_ms: 3000,
		engaged: true,
		beat_length: null
	});
	assert.equal(loops.safetyLoopToEngage([], { ...slot, out_ms: 70_000 }, 60_000), null);
});

test('a grid safety slot still engages phase-locked', () => {
	const slot = { in_ms: 1000, out_ms: 2000, beat_length: 2, armed: true };
	assert.deepEqual(loops.safetyLoopToEngage(GRID, slot, 60_000), loops.phaseLockedSafetyLoop(GRID, slot, 60_000));
});

test('playback crossing the active loop out engages it', () => {
	const safety = loops.activeLoopSafetySlot([cue({ active_loop: true })], 60_000);
	const reached = loops.playbackReachedSafetyLoopOut({
		active: true,
		startPositionSec: 0.5,
		startContextTime: 10,
		tempoRatio: 1,
		atContextTime: 13,
		safety,
		liveLoop: null
	});
	assert.equal(reached, true);
	assert.equal(loops.safetyLoopToEngage([], safety, 60_000).in_ms, 1000);
});

test('an explicit exit only disarms the active slot', () => {
	const safety = loops.activeLoopSafetySlot([cue({ active_loop: true })], 60_000);
	assert.deepEqual(loops.disarmSafetyLoopOnExplicitExit(safety), { ...safety, armed: false });
});

test('storedLoops lists the active memory row first and allows Set active only on memory rows', () => {
	const rows = loopSource.storedLoops([
		cue({ in_ms: 500, out_ms: 900 }),
		cue({ in_ms: 5000, out_ms: 7000, active_loop: true }),
		cue({ kind: 'hot_cue', slot: 'A', in_ms: 100, out_ms: 400 })
	]);
	const memory = rows.filter((row) => row.source === 'memory');
	assert.equal(memory[0].in_ms, 5000);
	assert.equal(memory[0].active, true);
	assert.ok(memory.every((row) => row.canBeActive));
	assert.equal(rows.find((row) => row.source === 'hot_cue').canBeActive, false);
});
