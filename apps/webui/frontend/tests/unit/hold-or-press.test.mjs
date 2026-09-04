/**
 * Generic hold-vs-press gesture primitive (LIBUX-05 / LIBUX-04 shared
 * mechanism).
 *
 * Real setTimeout/clearTimeout throughout, no injected fake timer: the
 * primitive exposes no seam for one (AGENTS.md's no-mocks rule; a fake timer
 * substituting for the real scheduler never exercises the code path
 * production actually runs). Threshold is kept short so the real waits stay
 * fast.
 *
 * Regression lines:
 * - if a quick tap fires onHoldStart instead of onPress then cmd+R's
 *   "press = toggle" reading breaks and every tap peeks instead of toggling
 * - if a sustained hold never fires onHoldStart then cmd+R's "hold = doesn't
 *   stay" reading breaks and the overlay can never be peeked
 * - if a hold's keyup fires onPress in addition to onHoldEnd then releasing
 *   a peek also toggles the persistent mode, undoing itself immediately
 * - if a hold-only gesture (no onPress, e.g. Opt) waits for the threshold
 *   before revealing then it lags behind every real key hold
 * - if a second keydown while already down restarts the timer then OS key
 *   repeat re-arms the hold window forever and it never fires
 * - if cancel() fires onPress/onHoldEnd, or keyDown still reports down after
 *   it, a blurred window resolves a gesture it should have abandoned
 */

import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/gestures/hold-or-press.ts');
});

const THRESHOLD_MS = 20;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('a quick tap (keyup before threshold) fires onPress, never onHoldStart', async () => {
	const calls = [];
	const gesture = mod.createHoldOrPress({
		holdThresholdMs: THRESHOLD_MS,
		onPress: () => calls.push('press'),
		onHoldStart: () => calls.push('hold-start'),
		onHoldEnd: () => calls.push('hold-end')
	});
	gesture.keyDown();
	assert.equal(gesture.down, true);
	assert.equal(gesture.holding, false);
	gesture.keyUp();
	assert.deepEqual(calls, ['press']);
	assert.equal(gesture.down, false);
	// The threshold timer must have been cleared by keyUp, not merely
	// outraced - wait past it and confirm nothing else fires.
	await wait(THRESHOLD_MS * 2);
	assert.deepEqual(calls, ['press'], 'the press must clear its own timer');
});

test('a sustained hold fires onHoldStart once the threshold elapses, onHoldEnd on release, never onPress', async () => {
	const calls = [];
	const gesture = mod.createHoldOrPress({
		holdThresholdMs: THRESHOLD_MS,
		onPress: () => calls.push('press'),
		onHoldStart: () => calls.push('hold-start'),
		onHoldEnd: () => calls.push('hold-end')
	});
	gesture.keyDown();
	await wait(THRESHOLD_MS * 2);
	assert.equal(gesture.holding, true);
	assert.deepEqual(calls, ['hold-start']);
	gesture.keyUp();
	assert.deepEqual(calls, ['hold-start', 'hold-end']);
});

test('a hold-only gesture (no onPress) fires onHoldStart immediately, no threshold wait', () => {
	const calls = [];
	const gesture = mod.createHoldOrPress({
		holdThresholdMs: THRESHOLD_MS, // ignored: no onPress means every keydown is a hold
		onHoldStart: () => calls.push('hold-start'),
		onHoldEnd: () => calls.push('hold-end')
	});
	gesture.keyDown();
	assert.deepEqual(calls, ['hold-start'], 'must not wait for a timer that is never armed');
	gesture.keyUp();
	assert.deepEqual(calls, ['hold-start', 'hold-end']);
});

test('repeated keydown while already down is a no-op (OS auto-repeat cannot re-arm the timer)', async () => {
	const calls = [];
	const gesture = mod.createHoldOrPress({
		holdThresholdMs: THRESHOLD_MS,
		onPress: () => calls.push('press'),
		onHoldStart: () => calls.push('hold-start'),
		onHoldEnd: () => calls.push('hold-end')
	});
	gesture.keyDown();
	gesture.keyDown();
	gesture.keyDown();
	await wait(THRESHOLD_MS * 2);
	assert.deepEqual(calls, ['hold-start']);
	gesture.keyUp();
	assert.deepEqual(calls, ['hold-start', 'hold-end']);
});

test('keyup with no matching keydown is a no-op', () => {
	const calls = [];
	const gesture = mod.createHoldOrPress({
		holdThresholdMs: THRESHOLD_MS,
		onPress: () => calls.push('press'),
		onHoldStart: () => calls.push('hold-start'),
		onHoldEnd: () => calls.push('hold-end')
	});
	gesture.keyUp();
	assert.deepEqual(calls, []);
});

test('a timer that fires after keyup already resolved the press does nothing (mutation guard)', async () => {
	// This is the case a naive implementation gets backwards: if the timer
	// callback does not re-check `down`, a keyup that races the timer would
	// fire onHoldStart AFTER onPress already ran for the same press.
	const calls = [];
	const gesture = mod.createHoldOrPress({
		holdThresholdMs: THRESHOLD_MS,
		onPress: () => calls.push('press'),
		onHoldStart: () => calls.push('hold-start'),
		onHoldEnd: () => calls.push('hold-end')
	});
	gesture.keyDown();
	gesture.keyUp(); // clears the timer in the real implementation
	assert.deepEqual(calls, ['press']);
	await wait(THRESHOLD_MS * 2);
	assert.deepEqual(calls, ['press'], 'keyUp must clear the pending hold timer');
});

test('cancel() during the pending window abandons the gesture silently - no onPress, no onHoldStart', async () => {
	const calls = [];
	const gesture = mod.createHoldOrPress({
		holdThresholdMs: THRESHOLD_MS,
		onPress: () => calls.push('press'),
		onHoldStart: () => calls.push('hold-start'),
		onHoldEnd: () => calls.push('hold-end')
	});
	gesture.keyDown();
	gesture.cancel();
	assert.equal(gesture.down, false);
	assert.equal(gesture.holding, false);
	await wait(THRESHOLD_MS * 2);
	assert.deepEqual(calls, [], 'a blur mid-press must not resolve as either a press or a hold');
});

test('cancel() with no keydown in flight is a no-op', () => {
	const calls = [];
	const gesture = mod.createHoldOrPress({
		holdThresholdMs: THRESHOLD_MS,
		onPress: () => calls.push('press'),
		onHoldStart: () => calls.push('hold-start'),
		onHoldEnd: () => calls.push('hold-end')
	});
	gesture.cancel();
	assert.deepEqual(calls, []);
});
