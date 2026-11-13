import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * CUES-13202 - a malformed saved active memory loop must never arm the
 * deck's safety slot (activeLoopSafetySlot returns null, so the engine leaves
 * the slot untouched), while a valid active memory loop still arms.
 *
 * Regression lines:
 * - if an active loop with no valid out point (null, missing, NaN, Infinity) arms the slot then broken
 * - if an active loop with an inverted or zero-length range arms the slot then broken
 * - if an active loop with a non-finite or negative in point arms the slot then broken
 * - if an invalid active loop hides a later valid active loop then broken
 * - if a valid active memory loop stops arming as a stored safety slot then broken
 * - if a stored slot with an inverted range engages through safetyLoopToEngage then broken
 */

let loops;

const DURATION_MS = 60_000;

function cue(over) {
	return {
		kind: 'loop',
		slot: null,
		in_ms: 1000,
		out_ms: 3000,
		is_loop: true,
		active_loop: true,
		beat_loop_size: null,
		color_table_index: null,
		comment: null,
		...over
	};
}

before(async () => {
	loops = await loadTypeScriptModule('src/lib/player/transport/loops.ts');
});

test('an active loop with no valid out point is ignored', () => {
	// Null, missing and non-finite out points must all be refused.
	assert.equal(loops.activeLoopSafetySlot([cue({ out_ms: null })], DURATION_MS), null);
	const missing = cue({});
	delete missing.out_ms;
	assert.equal(loops.activeLoopSafetySlot([missing], DURATION_MS), null);
	assert.equal(loops.activeLoopSafetySlot([cue({ out_ms: Number.NaN })], DURATION_MS), null);
	assert.equal(
		loops.activeLoopSafetySlot([cue({ out_ms: Number.POSITIVE_INFINITY })], DURATION_MS),
		null
	);
});

test('an active loop with an inverted or empty range is ignored', () => {
	// out before in, or out equal to in, is not a playable loop.
	assert.equal(loops.activeLoopSafetySlot([cue({ in_ms: 5000, out_ms: 2000 })], DURATION_MS), null);
	assert.equal(loops.activeLoopSafetySlot([cue({ in_ms: 4000, out_ms: 4000 })], DURATION_MS), null);
});

test('an active loop with a bad in point is ignored', () => {
	// A negative or non-finite in point must not arm the slot.
	assert.equal(loops.activeLoopSafetySlot([cue({ in_ms: -10, out_ms: 2000 })], DURATION_MS), null);
	assert.equal(
		loops.activeLoopSafetySlot([cue({ in_ms: Number.NaN, out_ms: 2000 })], DURATION_MS),
		null
	);
});

test('a valid active memory loop still arms after an invalid one', () => {
	// The invalid cue is skipped, the next valid active loop arms as saved.
	const cues = [cue({ in_ms: 5000, out_ms: 2000 }), cue({ in_ms: 8000, out_ms: 9000 })];
	assert.deepEqual(loops.activeLoopSafetySlot(cues, DURATION_MS), {
		in_ms: 8000,
		out_ms: 9000,
		beat_length: null,
		armed: true
	});
});

test('a valid active memory loop loads as an armed stored slot that engages as saved', () => {
	// Happy path: the loaded loop arms and engages exactly at its saved range.
	const slot = loops.activeLoopSafetySlot([cue({ in_ms: 1000, out_ms: 3000 })], DURATION_MS);
	assert.deepEqual(slot, { in_ms: 1000, out_ms: 3000, beat_length: null, armed: true });
	assert.deepEqual(loops.safetyLoopToEngage([], slot, DURATION_MS), {
		in_ms: 1000,
		out_ms: 3000,
		engaged: true,
		beat_length: null
	});
});

test('a stored slot with an inverted range never engages', () => {
	// Defence in depth: the engage path refuses an inverted stored slot too.
	const slot = { in_ms: 5000, out_ms: 2000, beat_length: null, armed: true };
	assert.equal(loops.safetyLoopToEngage([], slot, DURATION_MS), null);
	assert.equal(
		loops.safetyLoopToEngage([], { ...slot, in_ms: 2000, out_ms: Number.NaN }, DURATION_MS),
		null
	);
});
