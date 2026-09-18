// requirement: CUEOUT-01
// [if] MIX is at 0 and the operator single-clicks [then] MIX steps to 1/3, else stop.
// [if] repeated single-clicks from 0 [then] MIX follows 0,1/3,2/3,1,2/3,1/3,0, else stop.
// [if] a double-click lands on MIX [then] no delayed single-click step fires, else stop.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createDeferredClickGuard } from '../../src/lib/rb/deferred-click.ts';
import {
	HEADPHONE_MIX_STEP,
	stepHeadphoneMix
} from '../../src/lib/rb/headphone-mix-step.ts';

const THIRD = HEADPHONE_MIX_STEP;

function runSequence(startValue, clickCount, startDirection = 1) {
	let value = startValue;
	let direction = startDirection;
	const values = [value];
	for (let i = 0; i < clickCount; i += 1) {
		const stepped = stepHeadphoneMix(value, direction);
		value = stepped.value;
		direction = stepped.direction;
		values.push(value);
	}
	return values;
}

test('single clicks from 0 follow the full bounce sequence', () => {
	const values = runSequence(0, 6);
	assert.deepEqual(values, [0, THIRD, 2 * THIRD, 1, 2 * THIRD, THIRD, 0]);
});

test('starting at 1 with positive direction reverses before the next step', () => {
	const stepped = stepHeadphoneMix(1, 1);
	assert.equal(stepped.value, 2 * THIRD);
	assert.equal(stepped.direction, -1);
});

test('starting at 0 with negative direction reverses before the next step', () => {
	const stepped = stepHeadphoneMix(0, -1);
	assert.equal(stepped.value, THIRD);
	assert.equal(stepped.direction, 1);
});

test('arbitrary values advance by exactly one third, not snapped to thirds', () => {
	const stepped = stepHeadphoneMix(0.2, 1);
	assert.equal(stepped.value, 0.2 + THIRD);
	assert.equal(stepped.direction, 1);
});

test('double-click cancels a pending single-click before it fires', () => {
	const pending = new Map();
	let nextHandle = 0;
	const scheduler = {
		schedule(callback, delayMs) {
			const handle = nextHandle++;
			pending.set(handle, { callback, delayMs });
			return handle;
		},
		cancel(handle) {
			pending.delete(handle);
		}
	};
	const guard = createDeferredClickGuard(scheduler, 500);
	const fired = [];

	guard.schedule(() => fired.push('step'));
	assert.equal(pending.size, 1, 'first click should schedule a deferred step');

	guard.cancel();
	assert.equal(pending.size, 0, 'double-click must clear the pending callback');
	assert.deepEqual(fired, [], 'no step should fire when double-click cancels the first click');
});

test('a second schedule replaces the first pending single-click', () => {
	const pending = new Map();
	let nextHandle = 0;
	const scheduler = {
		schedule(callback, delayMs) {
			const handle = nextHandle++;
			pending.set(handle, { callback, delayMs });
			return handle;
		},
		cancel(handle) {
			pending.delete(handle);
		}
	};
	const guard = createDeferredClickGuard(scheduler, 500);
	const fired = [];

	guard.schedule(() => fired.push('first'));
	guard.schedule(() => fired.push('second'));
	assert.equal(pending.size, 1, 'only one pending single-click may exist');

	const [, onlyPending] = [...pending.entries()][0];
	onlyPending.callback();
	assert.deepEqual(fired, ['second']);
});
