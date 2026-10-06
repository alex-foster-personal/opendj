import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// H15 - wheel over any dial, fader or waveform adjusts that control by ONE
// fixed step. The module's own docstring carried these acceptance criteria with
// no test behind them; a docstring is a promise, not a gate.
//
// Regression lines:
// - if one notch stops moving the value by exactly params.step then broken
// - if the step is scaled by deltaY magnitude then a trackpad momentum burst
//   slams the control end to end - broken
// - if values are not clamped to 0..1 then out-of-range values reach AudioNodes
// - if a disabled control still calls set() then an inert control changes engine state
// - if a disabled control still calls preventDefault() then the page stops scrolling
// - if destroy() leaves the listener attached then a torn-down deck keeps reacting

let wheelAdjust;
let wheelDirection;
let WHEEL_STEP;

before(async () => {
	({ wheelAdjust, wheelDirection, WHEEL_STEP } = await loadTypeScriptModule(
		'src/lib/rb/wheel-adjust.ts'
	));
});

/** A real dispatchable wheel event: Node's Event plus the two delta fields. */
function wheelEvent(deltaY, deltaX = 0) {
	const event = new Event('wheel', { cancelable: true });
	event.deltaY = deltaY;
	event.deltaX = deltaX;
	return event;
}

/** A real EventTarget standing in for the control's DOM node. */
function control({ value = 0.5, step = 0.028, disabled = undefined } = {}) {
	const node = new EventTarget();
	const sets = [];
	let current = value;
	const params = {
		step,
		get: () => current,
		set: (next) => {
			sets.push(next);
			current = next;
		},
		disabled
	};
	const handle = wheelAdjust(node, params);
	return {
		node,
		handle,
		sets,
		params,
		get value() {
			return current;
		},
		turn(deltaY, deltaX = 0) {
			const event = wheelEvent(deltaY, deltaX);
			node.dispatchEvent(event);
			return event;
		}
	};
}

// -------------------------------------------------------- one fixed step

test('one wheel notch moves the value by exactly one step and swallows the page scroll', () => {
	const c = control({ value: 0.5, step: WHEEL_STEP.knob });

	const up = c.turn(-1); // wheel up = negative deltaY
	assert.equal(c.sets.length, 1);
	assert.ok(
		Math.abs(c.value - (0.5 + WHEEL_STEP.knob)) < 1e-12,
		`one notch up must move by exactly WHEEL_STEP.knob, landed at ${c.value}`
	);
	assert.equal(up.defaultPrevented, true, 'the panel underneath must not scroll');

	const down = c.turn(1);
	assert.ok(
		Math.abs(c.value - 0.5) < 1e-12,
		`one notch back down must return to the start, landed at ${c.value}`
	);
	assert.equal(down.defaultPrevented, true);
});

test('a trackpad momentum burst moves the same amount as a single mouse notch', () => {
	// The whole reason the module is direction-only: deltaY magnitudes vary
	// wildly between devices and momentum phases.
	const gentle = control({ value: 0.5, step: 0.028 });
	const violent = control({ value: 0.5, step: 0.028 });

	gentle.turn(-1);
	violent.turn(-1247.5);

	assert.equal(
		gentle.value,
		violent.value,
		`deltaY -1 landed at ${gentle.value} but deltaY -1247.5 landed at ${violent.value} - ` +
			'the step is being scaled by wheel magnitude'
	);
	assert.equal(violent.sets.length, 1);
});

test('direction is read as -1/0/+1 only, vertical winning over shift-scroll', () => {
	assert.equal(wheelDirection(wheelEvent(-1)), 1);
	assert.equal(wheelDirection(wheelEvent(1)), -1);
	assert.equal(wheelDirection(wheelEvent(-9999)), 1);
	assert.equal(wheelDirection(wheelEvent(0, -3)), 1, 'deltaX covers shift-scroll');
	assert.equal(wheelDirection(wheelEvent(0, 3)), -1);
	assert.equal(wheelDirection(wheelEvent(0, 0)), 0);
});

test('a zero-delta wheel event changes nothing and lets the page scroll', () => {
	const c = control({ value: 0.5 });
	const event = c.turn(0, 0);
	assert.deepEqual(c.sets, []);
	assert.equal(event.defaultPrevented, false);
});

// ------------------------------------------------------------- 0..1 clamp

test('turning past either end pins to 0 and 1, never outside', () => {
	const top = control({ value: 0.97, step: 0.028 });
	for (let i = 0; i < 12; i++) top.turn(-1);
	assert.equal(top.value, 1, `value pinned above 1 at ${top.value}`);
	assert.ok(
		top.sets.every((v) => v >= 0 && v <= 1),
		`every set() must stay inside 0..1, saw ${JSON.stringify(top.sets)}`
	);

	const bottom = control({ value: 0.02, step: 0.028 });
	for (let i = 0; i < 12; i++) bottom.turn(1);
	assert.equal(bottom.value, 0, `value pinned below 0 at ${bottom.value}`);
	assert.ok(
		bottom.sets.every((v) => v >= 0 && v <= 1),
		`every set() must stay inside 0..1, saw ${JSON.stringify(bottom.sets)}`
	);
});

test('a coarse step still clamps rather than overshooting', () => {
	const c = control({ value: 0.9, step: WHEEL_STEP.crossfader });
	c.turn(-1);
	c.turn(-1);
	c.turn(-1);
	assert.equal(c.value, 1);
});

// ------------------------------------------------------- disabled controls

test('a disabled control never calls set() and never blocks the page scroll', () => {
	const c = control({ value: 0.5, disabled: true });
	const event = c.turn(-1);
	assert.deepEqual(c.sets, [], 'an inert control must not change engine state');
	assert.equal(c.value, 0.5);
	assert.equal(
		event.defaultPrevented,
		false,
		'a disabled control must let the page scroll normally'
	);
});

test('update() re-arms and dis-arms the same node without re-binding', () => {
	const c = control({ value: 0.5, disabled: true });
	c.turn(-1);
	assert.deepEqual(c.sets, []);

	c.handle.update({ ...c.params, disabled: false });
	c.turn(-1);
	assert.equal(c.sets.length, 1);

	c.handle.update({ ...c.params, disabled: true });
	c.turn(-1);
	assert.equal(c.sets.length, 1, 'update(disabled) must stop further set() calls');
});

test('destroy() detaches the listener so a torn-down control goes inert', () => {
	const c = control({ value: 0.5 });
	c.turn(-1);
	assert.equal(c.sets.length, 1);
	c.handle.destroy();
	const event = c.turn(-1);
	assert.equal(c.sets.length, 1, 'destroyed action must not keep adjusting');
	assert.equal(event.defaultPrevented, false);
});

// --------------------------------------------------------- fail-fast params

test('a non-positive or non-finite step is refused at bind and at update', () => {
	for (const step of [0, -0.01, Number.NaN, Number.POSITIVE_INFINITY]) {
		assert.throws(
			() => wheelAdjust(new EventTarget(), { step, get: () => 0, set: () => {} }),
			RangeError,
			`step ${step} must be refused`
		);
	}
	const c = control();
	assert.throws(() => c.handle.update({ ...c.params, step: 0 }), RangeError);
});

test('missing get/set is refused rather than silently no-oping', () => {
	assert.throws(
		() => wheelAdjust(new EventTarget(), { step: 0.028, set: () => {} }),
		TypeError
	);
	assert.throws(
		() => wheelAdjust(new EventTarget(), { step: 0.028, get: () => 0 }),
		TypeError
	);
});

// ------------------------------------------------------------- step table

test('every control type has a usable step, and the coarse/fine ordering holds', () => {
	assert.deepEqual(Object.keys(WHEEL_STEP).sort(), ['crossfader', 'fader', 'knob', 'pitch']);
	for (const [name, step] of Object.entries(WHEEL_STEP)) {
		assert.ok(
			Number.isFinite(step) && step > 0 && step < 1,
			`WHEEL_STEP.${name} must be a usable 0..1 increment, got ${step}`
		);
	}
	assert.ok(
		WHEEL_STEP.crossfader > WHEEL_STEP.knob,
		'the crossfader travels a shorter useful distance, so its notch is coarser'
	);
	assert.ok(
		WHEEL_STEP.pitch < WHEEL_STEP.knob,
		'the pitch fader needs finer resolution than a dial'
	);
});

// -------------------------------------------------------- MIXUX-13 nudge

test('nudge receives the raw signed step, even when the control is pinned at an end', () => {
	// A multi-selected dial pinned at 1 must still pass the step on, or the
	// rest of its selection freezes (Sol P1 on #5701).
	const node = new EventTarget();
	const deltas = [];
	const sets = [];
	wheelAdjust(node, {
		step: 0.028,
		get: () => 1,
		set: (next) => sets.push(next),
		nudge: (delta) => deltas.push(delta)
	});
	const up = wheelEvent(-100);
	node.dispatchEvent(up);
	node.dispatchEvent(wheelEvent(100));
	assert.equal(deltas.length, 2);
	assert.ok(deltas[0] > 0, `wheel up at the top end reached nudge as ${deltas[0]}, not a positive step`);
	assert.ok(deltas[1] < 0);
	assert.equal(sets.length, 0, 'set() must not also run when nudge is given');
	assert.ok(up.defaultPrevented, 'the page scrolled underneath a nudged control');
});

test('without nudge, set() still gets the clamped value as before', () => {
	const c = control({ value: 1 });
	c.turn(-100);
	assert.deepEqual(c.sets, [1]);
});
