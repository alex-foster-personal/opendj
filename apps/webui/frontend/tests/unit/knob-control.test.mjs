import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// H5 - the two-dial linked-mixing control, the headline past-parity capability.
//
//   "Shift click, click - two dials selected. go up, one goes up, the other goes
//    down, staggered by 20% so you don't lose the bass in the cross over. [...]
//    The biggest limitation of transitions without physical dials to twist with
//    two hands - fixed."
//
// This was a LIVE REGRESSION: knob-control.svelte.ts existed with all the maths
// and had ZERO importers, and its only test lived on an unmerged branch. The
// module is now wired into Knob.svelte; this file guards both the maths and the
// wiring, because either alone leaves the feature dead.
//
// Regression lines:
// - if the stagger is dropped then bass dips through the crossover, the exact
//   failure the feature exists to prevent
// - if the stagger lands on the falling side then the cut is over-cut
// - if the link is not inverse then two dials just move together
// - if a linked drag accumulates stagger per pointermove frame then a slow drag
//   ends somewhere a fast one does not
// - if the global wheel is not bound, or ignores the selection, then "you still
//   have scroll wheel control attached" is gone
// - if the global wheel is bound while NO knob is selected then every wheel
//   event on the page waits on the main thread and the library list stops
//   scrolling on the compositor (LIBM-174, PERF-UI-13, issue #10227)
// - if the global wheel stops scaling by input kind then the shift-selected
//   dial is hypersensitive on a trackpad while every use:wheelAdjust control
//   is calm, i.e. the "global" sensitivity fix has a hole in it
// - if horizontal drag is not much less sensitive than vertical then fine adjust
//   is impossible
// - if Knob.svelte stops importing knob-control then the module is dead code
//   again and a refactor deletes the headline feature
// - if a pickup-armed dial drops its scoped .sr-only rule or moves the hardware
//   hint off the live title then the yellow ghost is the only cue (issue #9547)

const MIXER = fileURLToPath(new URL('../../src/lib/components/rb/mixer', import.meta.url));

let knobs;
/** wheel-adjust, for WHEEL_TRACKPAD_EVENTS_PER_DETENT only. */
let wheel;
let sens;
/** Registered fake dials by id, each holding a real settable value. */
let dials;

/**
 * The window wheel listener as bound RIGHT NOW. It exists only while a
 * selection does (PERF-UI-13), so it is looked up per call, never cached at
 * registration. With no selection the page scrolls natively: nothing runs and
 * nothing is prevented.
 */
function wheelHandler(event) {
	const fn = globalThis.__listeners.get('wheel');
	if (fn) fn(event);
}

/** The wheel guard tests `target instanceof HTMLElement`, so node needs one. */
class FakeHTMLElement {
	constructor(tagName, { isContentEditable = false } = {}) {
		this.tagName = tagName;
		this.isContentEditable = isContentEditable;
	}
}

before(async () => {
	// The module binds its window listeners when a selection appears (PERF-UI-13).
	const listeners = new Map();
	const store = new Map();
	globalThis.window = {
		addEventListener: (type, fn, options) => {
			const capture = options === true || options?.capture === true;
			listeners.set(capture ? `${type}:capture` : type, fn);
			globalThis.__listenerCalls?.push(['add', type, options]);
		},
		removeEventListener: (type, _fn, options) => {
			const capture = options === true || options?.capture === true;
			listeners.delete(capture ? `${type}:capture` : type);
			globalThis.__listenerCalls?.push(['remove', type, options]);
		},
		// knob-control now pulls in wheel-adjust for the trackpad scaling, and
		// that module reads its persisted factors at import time.
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	globalThis.HTMLElement = FakeHTMLElement;
	knobs = await loadTypeScriptModule('src/lib/rb/knob-control.svelte.ts');
	// Read-only: this is a SEPARATE module instance from the copy esbuild
	// bundled into knob-control, so only its constants are meaningful here.
	wheel = await loadTypeScriptModule('src/lib/rb/wheel-adjust.ts');
	// Read-only for the same reason: only its pure functions and defaults.
	sens = await loadTypeScriptModule('src/lib/rb/drag-sensitivity.svelte.ts');
	globalThis.__listeners = listeners;
});

beforeEach(() => {
	knobs.clearKnobLink();
	knobs.knobUi.selectedIds = [];
	knobs.knobUi.hoveredId = null;
	knobs._setCommandedGlideStarterForTests(() => {});
	// _registry is module-level state, not reset by the clears above - without
	// this, a dial id reused across test cases (every case below reuses
	// '1:low'/'2:low') carries a leftover registration stack into the next
	// test and silently changes its unregisterKnob() behavior.
	knobs._resetKnobRegistryForTests();
	dials = new Map();
	globalThis.__listenerCalls = [];
});

function addDial(id, value) {
	dials.set(id, { value });
	knobs.registerKnob({
		id,
		getValue: () => dials.get(id).value,
		setValue: (next) => {
			dials.set(id, { value: next });
		}
	});
	return id;
}

const wheelCalls = (kind) => globalThis.__listenerCalls.filter((c) => c[0] === kind && c[1] === 'wheel');

const valueOf = (id) => dials.get(id).value;

/** A wheel event as the window listener sees it. Defaults to a notched mouse
 * wheel: pixel deltaMode with wheelDeltaY quantized to a multiple of 120. That
 * is the device this listener always implicitly assumed, so every assertion
 * below that predates the trackpad scaling still describes a mouse. */
function wheelEvent(deltaY, target = null, { wheelDeltaY } = {}) {
	let prevented = false;
	return {
		deltaY,
		target,
		deltaMode: 0,
		wheelDeltaY: wheelDeltaY ?? (deltaY > 0 ? -120 : 120),
		preventDefault: () => {
			prevented = true;
		},
		wasPrevented: () => prevented
	};
}

/** One event out of a macOS trackpad two-finger burst: a small delta and a
 * wheelDeltaY that is NOT a multiple of 120. */
function trackpadTick(direction = 1, pixels = 2) {
	return wheelEvent(-pixels * direction, null, { wheelDeltaY: pixels * 1.2 * direction });
}

// ============================================ the stagger, in isolated maths

test('a linked turn moves the partner the other way and over-boosts the riser', () => {
	const out = knobs.applyLinkedDelta(0.5, 0.5, 0.1);

	assert.equal(out.secondary, 0.4, 'the partner did not move inversely by the same delta');
	assert.ok(
		out.primary > 0.6,
		`the riser must gain an extra stagger on top of +0.1, got ${out.primary} - ` +
			'without it the bass dips through the crossover'
	);
	assert.equal(
		Number(out.primary.toFixed(6)),
		Number((0.6 + 0.1 * knobs.KNOB_CFG.linkStagger).toFixed(6))
	);
});

test('the stagger follows the RISING side, whichever dial that is', () => {
	const down = knobs.applyLinkedDelta(0.5, 0.5, -0.1);

	assert.equal(down.primary, 0.4, 'the falling side must fall by exactly the delta');
	assert.ok(
		down.secondary > 0.6,
		`turning the primary DOWN must over-boost the partner, got ${down.secondary}`
	);
	assert.equal(
		Number(down.secondary.toFixed(6)),
		Number((0.6 + 0.1 * knobs.KNOB_CFG.linkStagger).toFixed(6))
	);
});

test('the stagger is a real over-boost, not a rounding artefact', () => {
	// The requirement is worth ~20%; anything near zero is the regression.
	assert.ok(
		knobs.KNOB_CFG.linkStagger >= 0.1,
		`linkStagger is ${knobs.KNOB_CFG.linkStagger} - too small to hold the bass up through a cross`
	);
	const total = knobs.applyLinkedDelta(0.5, 0.5, 0.2);
	assert.ok(
		total.primary + total.secondary > 1,
		'the pair must carry MORE combined energy mid-cross than it started with'
	);
});

test('a linked pair still respects the 0..1 domain at the extremes', () => {
	const out = knobs.applyLinkedDelta(0.95, 0.05, 0.2);
	assert.equal(out.primary, 1);
	assert.equal(out.secondary, 0);
});

// ============================================ the registry, end to end

test('turning one linked dial moves both through the registry', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.altClickKnob(a);
	knobs.altClickKnob(b);

	knobs.nudgeKnob(a, 0.1);

	assert.equal(valueOf(b), 0.4, 'the partner did not move');
	assert.ok(valueOf(a) > 0.6, 'the rising side lost its stagger');
	assert.equal(knobs.linkedPartnerId(a), b);
	assert.equal(knobs.linkedPartnerId(b), a);
});

test('an unlinked dial moves alone', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.nudgeKnob(a, 0.1);
	assert.equal(valueOf(a), 0.6, 'an unlinked dial must move by exactly the delta, no stagger');
	assert.equal(valueOf(b), 0.5);
});

test('a linked DRAG does not accumulate stagger across pointermove frames', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.altClickKnob(a);
	knobs.altClickKnob(b);

	// One coarse drag to 0.7 ...
	knobs.setKnobFromDrag(a, 0.5, 0.7, b, 0.5);
	const coarse = { a: valueOf(a), b: valueOf(b) };

	// ... versus the same drag delivered as many small frames from the SAME
	// pointer-down baselines, which is what a slow hand produces.
	dials.set(a, { value: 0.5 });
	dials.set(b, { value: 0.5 });
	for (const target of [0.55, 0.6, 0.63, 0.68, 0.7]) {
		knobs.setKnobFromDrag(a, 0.5, target, b, 0.5);
	}

	assert.equal(valueOf(a), coarse.a, 'a slow drag ended somewhere a fast one did not');
	assert.equal(valueOf(b), coarse.b, 'the stagger accumulated across frames');
});

test('an absolute set derives its delta, so the partner tracks it', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.altClickKnob(a);
	knobs.altClickKnob(b);

	knobs.setKnobAbsolute(a, 0.6);
	assert.ok(valueOf(b) < 0.5, 'setKnobAbsolute bypassed the link - the partner never moved');
});

test('unregistering half a pair clears the link rather than leaving a dangling partner', async () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.altClickKnob(a);
	knobs.altClickKnob(b);
	knobs.unregisterKnob(b);
	await Promise.resolve();

	assert.equal(knobs.linkedPartnerId(a), null, 'a is still linked to a dial that no longer exists');
	knobs.nudgeKnob(a, 0.1);
	assert.equal(valueOf(a), 0.6, 'the survivor must behave as an ordinary unlinked dial');
});

test('unregistering a shadowing registration (EqOverlay closing over a mixer knob) preserves the surviving link', () => {
	// Two widgets can register the SAME knob id at once - e.g. the mixer's
	// always-mounted low knob, shadowed by an EqOverlay's own copy of the same
	// dial while it is open. Closing the overlay must only pop ITS
	// registration, not clear state the still-mounted mixer knob is relying on.
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.altClickKnob(a);
	knobs.altClickKnob(b);

	knobs.registerKnob({
		id: a,
		getValue: () => dials.get(a).value,
		setValue: (next) => dials.set(a, { value: next })
	});
	knobs.unregisterKnob(a); // pops the shadow registration only

	assert.equal(
		knobs.linkedPartnerId(a),
		b,
		'the mixer knob underneath lost its link when only the shadow unregistered'
	);
	knobs.nudgeKnob(a, 0.1);
	assert.equal(
		valueOf(b),
		0.4,
		'the link must still drive the partner after the shadow registration is gone'
	);
});

// ============================================ selection + the global wheel

test('PERF-UI-13: registering knobs binds no window wheel listener; selecting binds one non-passive; clearing removes it', () => {
	for (const id of ['1:low', '2:low', '3:low', '1:mid']) addDial(id, 0.5);
	assert.deepEqual(wheelCalls('add'), [], 'mounting four knobs bound a window wheel listener (the library then scrolls on the main thread)');
	assert.deepEqual([...knobs._knobWindowListenersForTests()], [], 'the bound set is not empty at idle');
	assert.equal(globalThis.__listeners.has('wheel'), false);

	knobs.shiftClickKnob('1:low');
	assert.equal(wheelCalls('add').length, 1, 'selecting one knob did not bind exactly one wheel listener');
	assert.deepEqual(wheelCalls('add')[0][2], { passive: false }, 'the selection wheel must stay non-passive so it can preventDefault');
	assert.deepEqual([...knobs._knobWindowListenersForTests()].sort(), ['keydown', 'pointerdown', 'wheel']);

	knobs.shiftClickKnob('2:low');
	assert.equal(wheelCalls('add').length, 1, 'growing the selection re-bound the wheel');

	knobs.clearKnobSelection();
	assert.equal(wheelCalls('remove').length, 1, 'clearing the selection left the wheel listener bound');
	assert.deepEqual([...knobs._knobWindowListenersForTests()], []);
	assert.equal(globalThis.__listeners.has('wheel'), false, 'the fake window still holds a wheel listener');
	knobs.clearKnobSelection();
	assert.equal(wheelCalls('remove').length, 1, 'a clear on an empty selection removed again');
});

test('PERF-UI-13: setKnobSelection binds, and unmounting the last selected knob unbinds', async () => {
	addDial('1:low', 0.5);
	addDial('2:low', 0.5);
	knobs.setKnobSelection(['1:low']);
	assert.equal(wheelCalls('add').length, 1);
	knobs.setKnobSelection([]);
	assert.equal(wheelCalls('remove').length, 1, 'an empty setKnobSelection kept the wheel bound');

	knobs.setKnobSelection(['2:low']);
	assert.equal(wheelCalls('add').length, 2);
	knobs.unregisterKnob('2:low');
	// The prune is deferred one microtask (MIXUX-13), so the unbind is too.
	await new Promise((resolve) => queueMicrotask(resolve));
	assert.deepEqual([...knobs.selectedKnobIds()], []);
	assert.equal(wheelCalls('remove').length, 2, 'unmounting the last selected knob left the wheel bound');
	assert.deepEqual([...knobs._knobWindowListenersForTests()], []);
});

test('a shift-selected dial moves on a wheel event anywhere on the page', () => {
	const a = addDial('1:low', 0.5);
	addDial('2:low', 0.5);
	knobs.shiftClickKnob(a);
	assert.ok(knobs.isKnobSelected(a));

	// The pointer is nowhere near the dial - this is a page-level wheel.
	const event = wheelEvent(-100, new FakeHTMLElement('DIV'));
	wheelHandler(event);

	assert.equal(
		Number(valueOf(a).toFixed(6)),
		Number((0.5 + knobs.KNOB_CFG.scrollStep).toFixed(6)),
		'the global wheel did not reach the selected dial'
	);
	assert.ok(event.wasPrevented(), 'the page scrolled underneath instead');
	assert.equal(valueOf('2:low'), 0.5, 'an unselected dial moved');
});

test('the global wheel is direction-only, never magnitude-scaled', () => {
	const a = addDial('1:low', 0.5);
	knobs.shiftClickKnob(a);

	wheelHandler(wheelEvent(-4));
	const small = valueOf(a);
	dials.set(a, { value: 0.5 });
	wheelHandler(wheelEvent(-4000));

	assert.equal(
		valueOf(a),
		small,
		'a trackpad momentum burst slammed the control - the step is magnitude-scaled'
	);
});

test('a trackpad burst on the page-level wheel travels like one mouse detent', () => {
	const a = addDial('1:low', 0.5);
	knobs.shiftClickKnob(a);

	wheelHandler(wheelEvent(-100, new FakeHTMLElement('DIV')));
	const mouseTravel = valueOf(a) - 0.5;

	dials.set(a, { value: 0.5 });
	for (let i = 0; i < wheel.WHEEL_TRACKPAD_EVENTS_PER_DETENT; i += 1) {
		wheelHandler(trackpadTick(1));
	}
	const trackpadTravel = valueOf(a) - 0.5;

	assert.ok(mouseTravel > 0, `the mouse detent moved nothing (${mouseTravel})`);
	assert.ok(
		Math.abs(trackpadTravel - mouseTravel) < 1e-12,
		`trackpad burst moved ${trackpadTravel}, mouse detent moved ${mouseTravel} - the ` +
			'page-level wheel bypasses the global trackpad scaling'
	);
});

test('one trackpad tick moves the selected dial less than one mouse detent', () => {
	const a = addDial('1:low', 0.5);
	knobs.shiftClickKnob(a);

	wheelHandler(trackpadTick(1));
	const oneTick = valueOf(a) - 0.5;

	assert.ok(oneTick > 0, 'the trackpad tick did not move the dial at all');
	assert.ok(
		oneTick < knobs.KNOB_CFG.scrollStep,
		`one trackpad tick moved ${oneTick}, a full step is ${knobs.KNOB_CFG.scrollStep} - ` +
			'the page-level wheel is still taking a whole step per trackpad event'
	);
});

test('a linked pair both move on one global wheel notch', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.altClickKnob(a);
	knobs.altClickKnob(b);
	knobs.shiftClickKnob(a);

	wheelHandler(wheelEvent(-100));
	assert.ok(valueOf(a) > 0.5);
	assert.ok(valueOf(b) < 0.5, 'the wheel drove the selection but not its linked partner');
});

test('the global wheel yields to a focused text field', () => {
	const a = addDial('1:low', 0.5);
	knobs.shiftClickKnob(a);

	for (const tagName of ['INPUT', 'TEXTAREA']) {
		wheelHandler(wheelEvent(-100, new FakeHTMLElement(tagName)));
		assert.equal(valueOf(a), 0.5, `scrolling a ${tagName} moved a dial`);
	}
	wheelHandler(wheelEvent(-100, new FakeHTMLElement('DIV', { isContentEditable: true })));
	assert.equal(valueOf(a), 0.5, 'scrolling a contenteditable moved a dial');
});

test('nothing is selected means the wheel is left alone entirely', () => {
	const a = addDial('1:low', 0.5);
	const event = wheelEvent(-100);
	wheelHandler(event);
	assert.equal(valueOf(a), 0.5);
	assert.ok(!event.wasPrevented(), 'the page must scroll normally when no dial is selected');
});

test('shift-clicking the selected dial deselects it', () => {
	const a = addDial('1:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(a);
	assert.deepEqual(knobs.knobUi.selectedIds, []);
	wheelHandler(wheelEvent(-100));
	assert.equal(valueOf(a), 0.5, 'a deselected dial still answers the global wheel');
});

// ============================================ MIXUX-13: multi-select + two-axis drag
//
//   the maintainer, Tue 6 Oct 2026: "we had a feature where we could select multiple
//   knobs with shift held. on any click&drag then or scroll etc (as per usual
//   knob adjustments) it then adjusts both / all knobs up and down together
//   [...] up and right is both / all knobs up, up and left is one up, one down
//   [...] The white line showing a selected knob is great."
//
// Regression lines:
// - if shift+click stays a single selection then "all knobs together" is gone
// - if the selection loses its ORDER then the two-axis drag drives the wrong deck
// - if common mode does not clamp each dial then one pinned dial drags the rest
//   out of range or stops them
// - if horizontal drag drives the first-selected deck too then up+left cannot
//   split the decks
// - if one selected dial loses horizontal fine adjust then the existing single
//   knob feel regresses
// - if Esc or an empty click does not clear then a stale selection keeps
//   eating the global wheel

/** A DOM-ish target whose closest() answers from a fixed set of matching selectors. */
function fakeTarget(matches) {
	return {
		closest: (sel) =>
			matches.some((m) =>
				sel
					.split(',')
					.map((x) => x.trim())
					.includes(m)
			)
				? {}
				: null
	};
}

const near = (a, b) => Math.abs(a - b) < 1e-9;

test('toggleKnobSelection keeps selection order and toggles membership', () => {
	let sel = [];
	sel = knobs.toggleKnobSelection(sel, '1:low');
	sel = knobs.toggleKnobSelection(sel, '2:low');
	sel = knobs.toggleKnobSelection(sel, '1:high');
	assert.deepEqual(sel, ['1:low', '2:low', '1:high']);
	sel = knobs.toggleKnobSelection(sel, '2:low');
	assert.deepEqual(sel, ['1:low', '1:high'], 'removing the middle dial reordered the rest');
});

test('shift+click builds an ordered N-dial set, every member reads as selected', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	const c = addDial('1:high', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	knobs.shiftClickKnob(c);
	assert.deepEqual([...knobs.selectedKnobIds()], [a, b, c]);
	for (const id of [a, b, c]) assert.ok(knobs.isKnobSelected(id), `${id} lost its selected ring`);
	knobs.shiftClickKnob(b);
	assert.ok(!knobs.isKnobSelected(b), 'shift+click on a selected dial must take it out of the set');
	assert.deepEqual([...knobs.selectedKnobIds()], [a, c]);
});

test('common mode moves every dial by the same delta, each clamped to 0..1', () => {
	const out = knobs.commonModeTargets({ '1:low': 0.5, '2:low': 0.95, '3:low': 0.02 }, 0.1);
	assert.ok(near(out['1:low'], 0.6));
	assert.equal(out['2:low'], 1, 'a dial past the top must pin at 1');
	assert.ok(near(out['3:low'], 0.12), 'a dial near the bottom must still move by the full delta');
	const down = knobs.commonModeTargets({ '1:low': 0.05, '2:low': 0.5 }, -0.1);
	assert.equal(down['1:low'], 0, 'a dial past the bottom must pin at 0');
	assert.ok(near(down['2:low'], 0.4), 'one pinned dial stopped the others moving');
});

test('the global wheel moves every selected dial together, and nothing else', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.3);
	addDial('3:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	wheelHandler(wheelEvent(-100, new FakeHTMLElement('DIV')));
	const step = knobs.KNOB_CFG.scrollStep;
	assert.ok(near(valueOf(a), 0.5 + step), 'the first selected dial did not move');
	assert.ok(near(valueOf(b), 0.3 + step), 'the second selected dial did not move with the first');
	assert.equal(valueOf('3:low'), 0.5, 'an unselected dial moved');
});

test('scroll or arrow on a selected dial moves the whole selection; on an unselected dial only it', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	const c = addDial('3:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	knobs.nudgeKnobOrSelection(b, -0.1);
	assert.ok(
		near(valueOf(a), 0.4) && near(valueOf(b), 0.4),
		'the selection did not move in common mode'
	);
	knobs.nudgeKnobOrSelection(c, 0.1);
	assert.ok(near(valueOf(c), 0.6));
	assert.ok(near(valueOf(a), 0.4), 'turning an unselected dial dragged the selection along');
});

test('R7 pad: up+right is all up, down+right is channel A up and channel B down', () => {
	const sel = ['1:low', '2:low'];
	const base = { '1:low': 0.5, '2:low': 0.5 };
	const upRight = knobs.padDragTargets(sel, base, 0.1, 0.1);
	assert.ok(
		near(upRight['1:low'], 0.6) && near(upRight['2:low'], 0.6),
		`up+right ${JSON.stringify(upRight)}`
	);
	const downRight = knobs.padDragTargets(sel, base, 0.1, -0.1);
	assert.ok(
		near(downRight['1:low'], 0.6) && near(downRight['2:low'], 0.4),
		'the -45 degree diagonal is not the flip'
	);
	const xOnly = knobs.padDragTargets(sel, base, 0.1, 0);
	assert.ok(near(xOnly['2:low'], 0.5), 'pure X moved channel B - the axes are not independent');
	const yOnly = knobs.padDragTargets(sel, base, 0, 0.1);
	assert.ok(
		near(yOnly['1:low'], 0.5) && near(yOnly['2:low'], 0.6),
		'pure Y must move only channel B'
	);
});

test('R7 pad: X is every item on the first-selected channel; one channel puts the first on X and the rest on Y', () => {
	assert.deepEqual(knobs.padAxes(['2:low', '1:low', '2:high', '1:fader']), {
		x: ['2:low', '2:high'],
		y: ['1:low', '1:fader']
	});
	assert.deepEqual(knobs.padAxes(['1:mid', '1:low', '1:fader']), {
		x: ['1:mid'],
		y: ['1:low', '1:fader']
	});
});

test('R7 pad clamps, and one selected item is not a pad', () => {
	const out = knobs.padDragTargets(['1:low', '2:low'], { '1:low': 0.9, '2:low': 0.1 }, 2, -2);
	assert.equal(out['1:low'], 1);
	assert.equal(out['2:low'], 0);
	assert.equal(knobs.padDragTargets(['1:low'], { '1:low': 0.5 }, 0.5, 0), null);
	const fine = knobs.singleDragDelta(100, 0);
	const coarse = knobs.singleDragDelta(0, 100);
	assert.ok(
		fine > 0 && fine * 3 <= coarse,
		`horizontal ${fine} is not fine adjust next to vertical ${coarse}`
	);
});

test('a selection drag starts only on a selected dial of a 2+ set and never accumulates', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	const c = addDial('3:low', 0.5);
	knobs.shiftClickKnob(a);
	assert.equal(
		knobs.beginSelectionDrag(a),
		null,
		'one selected dial must keep the single-dial drag'
	);
	knobs.shiftClickKnob(b);
	assert.equal(
		knobs.beginSelectionDrag(c),
		null,
		'dragging an unselected dial must not move the selection'
	);
	const start = knobs.beginSelectionDrag(b);
	assert.ok(start !== null);
	for (const d of [0.02, 0.03, 0.03, 0.02]) knobs.stepSelectionDrag(start, d, false);
	assert.ok(
		near(valueOf(a), 0.6) && near(valueOf(b), 0.6),
		`slow drag ended at ${valueOf(a)}, ${valueOf(b)}`
	);
	assert.equal(valueOf(c), 0.5);
});

test('Esc clears the selection', () => {
	const a = addDial('1:low', 0.5);
	knobs.shiftClickKnob(a);
	globalThis.__listeners.get('keydown')({ key: 'Enter' });
	assert.ok(knobs.isKnobSelected(a), 'a non-Esc key cleared the selection');
	globalThis.__listeners.get('keydown')({ key: 'Escape' });
	assert.deepEqual([...knobs.selectedKnobIds()], []);
	const event = wheelEvent(-100);
	wheelHandler(event);
	assert.ok(!event.wasPrevented(), 'a cleared selection still eats the page wheel');
});

test('a click on empty space clears; a click on a knob or a control does not', () => {
	const a = addDial('1:low', 0.5);
	knobs.shiftClickKnob(a);
	const down = globalThis.__listeners.get('pointerdown:capture');
	down({ button: 0, target: fakeTarget(['[data-knob-id]']) });
	assert.ok(knobs.isKnobSelected(a), 'clicking a knob cleared the selection');
	down({ button: 0, target: fakeTarget(['button']) });
	assert.ok(knobs.isKnobSelected(a), 'clicking a button is not a click on empty space');
	down({ button: 2, target: fakeTarget([]) });
	assert.ok(knobs.isKnobSelected(a), 'a right click cleared the selection');
	down({ button: 0, target: fakeTarget([]) });
	assert.ok(!knobs.isKnobSelected(a), 'a click on empty space left the selection');
	assert.equal(knobs.isEmptySpaceTarget(null), false);
});

test('unmounting a selected dial drops it from the set', async () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	knobs.unregisterKnob(a);
	await Promise.resolve();
	assert.deepEqual([...knobs.selectedKnobIds()], [b]);
});

test('a dial re-registering its own id (value-change re-run) keeps its selection and link', async () => {
	// Found live: Knob.svelte's register effect re-runs while a dial turns, a
	// synchronous unregister+register of the same id. Clearing on that
	// unregister wiped the selection on the first frame of every drag.
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	const c = addDial('3:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	knobs.altClickKnob(b);
	knobs.altClickKnob(c);
	knobs.unregisterKnob(b);
	addDial(b, 0.5);
	await Promise.resolve();
	assert.deepEqual(
		[...knobs.selectedKnobIds()],
		[a, b],
		'a re-render dropped the dial from the selection'
	);
	assert.equal(knobs.linkedPartnerId(b), c, 'a re-render dropped the Alt link');
});

test('agent parity: window.__mdtKnobSelection drives the same selection and moves', () => {
	const bridge = globalThis.window.__mdtKnobSelection;
	assert.ok(bridge, 'the agent bridge is not installed');
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	bridge.set([a, b]);
	assert.deepEqual(bridge.get(), [a, b]);
	assert.ok(knobs.isKnobSelected(b), 'the bridge and the UI hold two selections');
	bridge.nudge(0.1);
	assert.ok(
		near(bridge.value(a), 0.6) && near(bridge.value(b), 0.6),
		'bridge nudge is not common mode'
	);
	const px = knobs.KNOB_CFG.dragVerticalPx * 0.1;
	// The bridge drag runs through the pointer's drag gain (Sol P1, PR #6829):
	// a straight drag scales both axes by one integrated gain g, so the pad
	// split is +/- 0.1 * g. g comes from a separate module instance at the
	// shipped defaults, which is what the bundled copy holds here.
	const gained = sens.gainedStraightDrag(-px, px, {
		base: sens.DRAG_DEFAULTS.base,
		falloffPx: sens.DRAG_DEFAULTS.falloffPx
	});
	const g = gained.dy / px;
	assert.ok(
		g > 0 && g < 0.99,
		`the default drag gain ${g} is not below 1, so this test cannot tell gained from raw`
	);
	bridge.drag(-px, px);
	assert.ok(
		near(valueOf(a), 0.6 - 0.1 * g) && near(valueOf(b), 0.6 + 0.1 * g),
		`bridge pad up+left gave ${valueOf(a)}, ${valueOf(b)}; the gained drag predicts ${0.6 - 0.1 * g}, ${0.6 + 0.1 * g}`
	);
	assert.throws(() => bridge.set(['9:nope']), /no mounted knob/);
	assert.deepEqual(bridge.get(), [a, b], 'a refused set changed the selection');
	assert.throws(() => bridge.nudge(Number.NaN), RangeError);
	bridge.clear();
	assert.deepEqual(bridge.get(), []);
});

// ============================================ sensitivity

test('horizontal drag is at least 3x less sensitive than vertical', () => {
	const ratio = knobs.KNOB_CFG.dragHorizontalPx / knobs.KNOB_CFG.dragVerticalPx;
	assert.ok(
		ratio >= 3,
		`horizontal drag is only ${ratio.toFixed(1)}x less sensitive than vertical - ` +
			'the fine-adjust axis is gone'
	);
});

// ============================================ R2: the dragged control leads (Wed 7 Oct 2026)

test('R2: a drag on the SECOND-selected dial moves the whole group with it', () => {
	// the maintainer: "the up/down direction is for the first one selected, not the one
	// click-dragged on". Mutation control: the old two-axis rule drove only the
	// first-selected deck on vertical travel, so the dragged dial stayed put.
	const a = addDial('1:low', 0.5);
	const b = addDial('3:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	const start = knobs.beginSelectionDrag(b);
	assert.equal(start.leadId, b, 'the dragged dial is not the lead');
	knobs.stepSelectionDrag(start, 0.2, false);
	assert.ok(near(valueOf(b), 0.7), `the dragged dial did not follow the pointer (${valueOf(b)})`);
	assert.ok(
		near(valueOf(a), 0.7),
		`the first-selected dial did not move the same way (${valueOf(a)})`
	);
	// The old rule: vertical travel moved only the first-selected deck, so b stayed at 0.5.
	assert.ok(
		!near(valueOf(b), 0.5),
		'control: b did not move, which is the old first-selected rule'
	);
});

test('R2: the lead is the dragged item, never selection[0], for the sign rule', () => {
	const sel = ['1:low', '2:low'];
	assert.deepEqual(knobs.leadDragSigns(sel, '2:low', false), { '1:low': 1, '2:low': 1 });
	// With the flip, only the lead's side follows; selection[0] as lead would invert this.
	assert.deepEqual(knobs.leadDragSigns(sel, '2:low', true), { '1:low': -1, '2:low': 1 });
});

test('R2: a group drag clamps each item but keeps its virtual travel, so reversing is exact', () => {
	const a = addDial('1:low', 0.95);
	const b = addDial('2:low', 0.2);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	const start = knobs.beginSelectionDrag(b);
	knobs.stepSelectionDrag(start, 0.2, false);
	assert.equal(valueOf(a), 1);
	assert.ok(near(valueOf(b), 0.4));
	knobs.stepSelectionDrag(start, -0.2, false);
	assert.ok(
		near(valueOf(a), 0.95) && near(valueOf(b), 0.2),
		'reversing the drag did not return both'
	);
});

// ============================================ the wiring (the live regression)

test('the Knob component routes its input through knob-control', () => {
	const source = readFileSync(`${MIXER}/Knob.svelte`, 'utf8');

	assert.match(
		source,
		/from '\$lib\/rb\/knob-control\.svelte'/,
		'Knob.svelte no longer imports knob-control - the headline feature is dead code again'
	);
	for (const [fn, why] of [
		['registerKnob', 'the dial is not in the registry, so the global wheel cannot find it'],
		['shiftClickKnob', 'shift+click no longer selects a dial'],
		['beginSelectionDrag', 'a drag on a selected dial no longer moves the whole selection'],
		['stepSelectionDrag', 'the group drag led by the dragged dial is not wired'],
		['trackDragMove', 'the drag bypasses the shared drag sensitivity'],
		['nudgeKnobOrSelection', 'scroll or arrow keys on a selected dial move only that dial'],
		['altClickKnob', 'alt+click no longer links a pair'],
		['setKnobFromDrag', 'a drag bypasses the link maths, so the partner never moves'],
		['setKnobAbsolute', 'the wheel or keyboard bypasses the link maths'],
		['unregisterKnob', 'an unmounted dial stays in the registry forever']
	]) {
		assert.ok(source.includes(`${fn}(`), `Knob.svelte does not call ${fn}: ${why}`);
	}
	assert.ok(
		source.includes('singleDragDelta('),
		'Knob.svelte kept a local drag range instead of the central sensitivity config'
	);
	assert.ok(
		!/DRAG_RANGE_PX/.test(source),
		'the old local DRAG_RANGE_PX is back - sensitivity has split into two sources'
	);
});

test('every rendered knob carries a registry id', () => {
	for (const file of ['ChannelStrip.svelte', 'HeadphoneCluster.svelte']) {
		const source = readFileSync(`${MIXER}/${file}`, 'utf8');
		const uses = [...source.matchAll(/<Knob\b[^>]*>/g)].map((m) => m[0]);
		assert.ok(uses.length > 0, `${file} renders no Knob - find where the dials moved to`);
		for (const use of uses) {
			assert.match(
				use,
				/knobId=\{knobId\(/,
				`a knob in ${file} has no registry id, so it can never be selected or linked:\n${use}`
			);
		}
	}
});

test('only the headphone MIX knob opts into delayed single-click stepping', () => {
	const headphone = readFileSync(`${MIXER}/HeadphoneCluster.svelte`, 'utf8');
	const channel = readFileSync(`${MIXER}/ChannelStrip.svelte`, 'utf8');
	const knob = readFileSync(`${MIXER}/Knob.svelte`, 'utf8');

	assert.match(headphone, /onsingleclick=\{handleMixSingleClick\}/);
	assert.match(headphone, /stepHeadphoneMix/);
	assert.doesNotMatch(channel, /onsingleclick=/);
	assert.match(knob, /createDeferredClickGuard/);
	assert.match(knob, /handleDblClick/);
	assert.match(knob, /setKnobFromDrag/);
	assert.match(knob, /pointerMoved/);
});

test('pointer jitter inside the click slop is a click, travel past it is a drag', () => {
	const slop = knobs.KNOB_CFG.clickSlopPx;
	assert.ok(slop >= 2 && slop <= 8, `click slop ${slop}px is outside a sane 2..8px band`);
	assert.equal(
		knobs.pointerTravelIsDrag(0, 0),
		false,
		'if a pointermove with no travel counts as a drag then a still click never steps MIX - broken'
	);
	assert.equal(
		knobs.pointerTravelIsDrag(1, -1),
		false,
		'if one pixel of mouse or touch jitter counts as a drag then the 1/3 step is lost - broken'
	);
	assert.equal(knobs.pointerTravelIsDrag(slop, 0), false);
	assert.equal(
		knobs.pointerTravelIsDrag(0, slop + 1),
		true,
		'if travel past the slop is still a click then a short drag also fires the step - broken'
	);
	assert.equal(knobs.pointerTravelIsDrag(-(slop + 1), 0), true);
});

test('the Knob classifies drags by travel and only steps on a primary click', () => {
	const knob = readFileSync(`${MIXER}/Knob.svelte`, 'utf8');
	assert.match(
		knob,
		/pointerTravelIsDrag\(/,
		'if Knob.svelte marks any pointermove as a drag then jitter swallows the MIX click - broken'
	);
	assert.match(
		knob,
		/\(onsingleclick !== undefined \|\| ontap !== undefined \|\| altPressInGroup\) && insideSlop/,
		'if the click slop applies to every knob then 1-4 px fine adjustments on EQ and GAIN are discarded - broken'
	);
	assert.match(
		knob,
		/e\.button !== 0/,
		'if a right or middle click starts a gesture then a context-menu click steps MIX - broken'
	);
});

test('knob ids are unique across the whole mixer', () => {
	// Two dials sharing an id would silently drive each other.
	const roles = ['trim', 'high', 'mid', 'low', 'filter'];
	const ids = [];
	for (const deck of [1, 2, 3, 4]) for (const role of roles) ids.push(knobs.knobId(deck, role));
	ids.push(knobs.knobId('hp', 'hp-mix'), knobs.knobId('hp', 'hp-level'));

	assert.equal(new Set(ids).size, ids.length, 'two mixer dials resolve to the same registry id');
});

test('a selected dial pinned at 1 still moves the rest of the selection on its own scroll', () => {
	const a = addDial('1:low', 1);
	const b = addDial('2:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	knobs.nudgeKnobOrSelection(a, 0.028);
	assert.equal(valueOf(a), 1);
	assert.ok(near(valueOf(b), 0.528), 'the pinned dial froze the rest of the selection');
});

test('the Knob hands the raw wheel step to the selection, not a delta from the clamped value', () => {
	const source = readFileSync(`${MIXER}/Knob.svelte`, 'utf8');
	assert.match(
		source,
		/nudge: \(delta, event\) => nudgeKnobOrSelection\(knobId, delta, event\.altKey\)/,
		'if the wheel derives its delta from the clamped value then a dial at 0 or 1 freezes the selection'
	);
});

test('control: with no selection, a dial own wheel step moves only that dial, clamped as before', () => {
	const a = addDial('1:low', 1);
	const b = addDial('2:low', 0.5);
	knobs.nudgeKnobOrSelection(a, 0.028);
	knobs.nudgeKnobOrSelection(b, -0.028);
	assert.equal(valueOf(a), 1, 'a single dial at the top end left the 0..1 domain');
	assert.ok(
		near(valueOf(b), 0.472),
		'a single unselected dial no longer moves by exactly one step'
	);
});

// Sol P1 on #5701 (dfca9f762): a group move bypassed the Alt link, so a
// linked pair could move the same way. The link must survive group moves.

test('a group nudge drives an unselected Alt partner inversely, with the stagger', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('1:high', 0.5);
	const p = addDial('2:low', 0.5);
	knobs.altClickKnob(a);
	knobs.altClickKnob(p);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	knobs.nudgeSelection(0.1);
	assert.ok(near(valueOf(b), 0.6), 'the unlinked selected dial lost common mode');
	assert.ok(near(valueOf(p), 0.4), `the Alt partner did not move inversely (${valueOf(p)})`);
	assert.ok(
		near(valueOf(a), 0.6 + 0.1 * knobs.KNOB_CFG.linkStagger),
		'the rising linked dial lost its stagger'
	);
});

test('both dials of a linked pair selected: the selection wins, both move together', () => {
	// CORE, orders board #5638 18:53Z: the maintainer's spec is "adjusts both / all knobs
	// up and down together"; the link is not applied inside the selection.
	const a = addDial('1:low', 0.5);
	const p = addDial('2:low', 0.5);
	knobs.altClickKnob(a);
	knobs.altClickKnob(p);
	knobs.shiftClickKnob(p);
	knobs.shiftClickKnob(a);
	knobs.nudgeSelection(0.1);
	assert.ok(
		near(valueOf(a), 0.6) && near(valueOf(p), 0.6),
		`a selected linked pair did not move together (${valueOf(a)}, ${valueOf(p)}) - the link overrode the selection`
	);
	const start = knobs.beginSelectionDrag(a);
	knobs.stepSelectionDrag(start, 0.1, true);
	assert.ok(
		near(valueOf(a), 0.7) && near(valueOf(p), 0.5),
		`the flipped drag on a selected linked pair gave ${valueOf(a)}, ${valueOf(p)}`
	);
});

test('a group drag keeps the Alt link too, from the pointer-down baselines', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:high', 0.5);
	const p = addDial('3:low', 0.5);
	knobs.altClickKnob(a);
	knobs.altClickKnob(p);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	const start = knobs.beginSelectionDrag(a);
	for (const d of [0.03, 0.03, 0.04]) knobs.stepSelectionDrag(start, d, false);
	assert.ok(
		near(valueOf(p), 0.4),
		`the partner did not track inversely without accumulating (${valueOf(p)})`
	);
	assert.ok(near(valueOf(b), 0.6), 'the other selected dial did not follow the lead');
});

test('control: with no link, group moves are plain common mode', () => {
	const out = knobs.applyLinkToTargets(
		['1:low', '2:low'],
		{ '1:low': 0.5, '2:low': 0.5 },
		{ '1:low': 0.6, '2:low': 0.6 },
		null
	);
	assert.deepEqual(out, { '1:low': 0.6, '2:low': 0.6 });
});

// ============================================ R3: Opt flips one side (Wed 7 Oct 2026)

test('R3: two channels, Opt held: the dragged channel follows, the other goes opposite', () => {
	const a1 = addDial('1:low', 0.5);
	const a2 = addDial('1:mid', 0.5);
	const b = addDial('2:low', 0.5);
	for (const id of [a1, a2, b]) knobs.shiftClickKnob(id);
	const start = knobs.beginSelectionDrag(b);
	knobs.stepSelectionDrag(start, 0.1, true);
	assert.ok(near(valueOf(b), 0.6), 'the dragged item did not follow the pointer');
	assert.ok(
		near(valueOf(a1), 0.4) && near(valueOf(a2), 0.4),
		'the other channel did not go opposite'
	);
});

test('R3 mutation control: the flip side comes from the DRAGGED channel, not the first-selected', () => {
	// Taking selection[0] as the lead would move deck 1 up and deck 2 down here.
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	const start = knobs.beginSelectionDrag(b);
	knobs.stepSelectionDrag(start, 0.1, true);
	assert.ok(near(valueOf(b), 0.6) && near(valueOf(a), 0.4), `got a=${valueOf(a)} b=${valueOf(b)}`);
	knobs.stepSelectionDrag(start, 0.1, false);
	assert.ok(near(valueOf(a), 0.5), 'without Opt every item follows');
});

test('R3: one channel, Opt held: the item under the pointer follows, the rest go opposite', () => {
	const low = addDial('1:low', 0.5);
	const mid = addDial('1:mid', 0.5);
	const fader = addDial(knobs.faderControlId(1), 0.5);
	for (const id of [low, mid, fader]) knobs.shiftClickKnob(id);
	const start = knobs.beginSelectionDrag(mid);
	knobs.stepSelectionDrag(start, 0.1, true);
	assert.ok(near(valueOf(mid), 0.6));
	assert.ok(near(valueOf(low), 0.4) && near(valueOf(fader), 0.4));
});

test('R3: releasing Opt mid-drag stops the flip from that point with no jump', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	const start = knobs.beginSelectionDrag(b);
	knobs.stepSelectionDrag(start, 0.1, true); // a 0.4, b 0.6
	const before = valueOf(a);
	knobs.stepSelectionDrag(start, 0.01, false);
	assert.ok(near(valueOf(a), before + 0.01), `a jumped from ${before} to ${valueOf(a)}`);
	assert.ok(near(valueOf(b), 0.61));
});

test('R3: the wheel with Opt flips relative to the control under the pointer, else the first-selected', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	knobs.nudgeKnobOrSelection(b, 0.1, true);
	assert.ok(near(valueOf(b), 0.6) && near(valueOf(a), 0.4));
	const onWheel = globalThis.__listeners.get('wheel');
	knobs.setKnobHovered(null);
	onWheel({
		deltaY: -100,
		deltaMode: 0,
		wheelDeltaY: 120,
		altKey: true,
		target: null,
		preventDefault() {}
	});
	// Lead falls back to the first-selected (a): a up, b down.
	assert.ok(valueOf(a) > 0.4 && valueOf(b) < 0.6, `got a=${valueOf(a)} b=${valueOf(b)}`);
});

test('R3 wiring: Knob and VFader pass e.altKey to the group drag and the wheel', () => {
	const knob = readFileSync(`${MIXER}/Knob.svelte`, 'utf8');
	assert.match(
		knob,
		/stepSelectionDrag\(selectionDrag, singleDragDelta\(step\.dx, step\.dy\), e\.altKey\)/
	);
	assert.match(knob, /nudgeKnobOrSelection\(knobId, delta, event\.altKey\)/);
	assert.match(
		knob,
		/if \(altClick\) \{\s*altClickKnob\(knobId\)/,
		'an Alt press on a grouped dial that never moves no longer arms a link'
	);
	const fader = readFileSync(`${MIXER}/VFader.svelte`, 'utf8');
	assert.match(fader, /stepSelectionDrag\(selectionDrag, delta, e\.altKey\)/);
	assert.match(fader, /setKnobHovered\(controlId\)/);
});

// ============================================ R4: at most two channels (Wed 7 Oct 2026)

test('R4: a third channel is refused with a hint; the first two stay selected', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	const c = addDial('3:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	assert.equal(knobs.shiftClickKnob(c), false);
	assert.deepEqual(
		[...knobs.selectedKnobIds()],
		[a, b],
		'a deck 3 item was added to a deck 1 + 2 group'
	);
	assert.deepEqual(knobs.knobUi.groupHint, { id: c, text: 'Group is limited to two channels' });
});

test('R4: any number of items on one or two channels are all selected', () => {
	const ids = ['1:low', '1:mid', '1:high', '1:fader', '2:low', '2:mid', '2:fader'].map((id) =>
		addDial(id, 0.5)
	);
	for (const id of ids) assert.equal(knobs.shiftClickKnob(id), true, `${id} was refused`);
	assert.deepEqual([...knobs.selectedKnobIds()], ids);
	assert.equal(knobs.knobUi.groupHint, null);
});

test('R4: removing an item is never refused, and freeing a channel lets a new one in', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	const c = addDial('3:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	assert.equal(knobs.shiftClickKnob(a), true);
	assert.equal(knobs.shiftClickKnob(c), true);
	assert.deepEqual([...knobs.selectedKnobIds()], [b, c]);
});

test('R4: the agent bridge refuses a three-channel selection loudly', () => {
	addDial('1:low', 0.5);
	addDial('2:low', 0.5);
	addDial('3:low', 0.5);
	assert.throws(
		() => globalThis.window.__mdtKnobSelection.set(['1:low', '2:low', '3:low']),
		/limited to two channels/
	);
});

// ============================================ R7: the two-axis pad (Wed 7 Oct 2026)

test('R7: a pad drag accumulates per axis, and the dot release leaves values where they were', () => {
	const a = addDial('1:low', 0.5);
	const a2 = addDial('1:fader', 0.5);
	const b = addDial('2:low', 0.5);
	for (const id of [a, a2, b]) knobs.shiftClickKnob(id);
	const start = knobs.beginPadDrag();
	assert.ok(start !== null);
	knobs.stepPadDrag(start, 0.05, 0);
	knobs.stepPadDrag(start, 0.05, -0.1);
	assert.ok(near(valueOf(a), 0.6) && near(valueOf(a2), 0.6), 'channel A did not follow X');
	assert.ok(near(valueOf(b), 0.4), 'channel B did not follow Y');
	// A fresh press starts from where the last one left off (relative pad).
	const again = knobs.beginPadDrag();
	knobs.stepPadDrag(again, 0, 0.1);
	assert.ok(near(valueOf(b), 0.5) && near(valueOf(a), 0.6));
});

test('R7: no pad drag below two selected items', () => {
	knobs.shiftClickKnob(addDial('1:low', 0.5));
	assert.equal(knobs.beginPadDrag(), null);
});

test('R7 wiring: the pad shows at 2+, closes and clears, uses the shared gain, and is mounted in the mixer', () => {
	const pad = readFileSync(`${MIXER}/GroupPad.svelte`, 'utf8');
	assert.match(pad, /knobUi\.selectedIds\.length >= 2/);
	assert.match(pad, /clearKnobSelection\(\)/);
	assert.match(pad, /e\.key === 'Escape' && pointerOver/);
	assert.match(pad, /trackDragMove\(tracker, e\.clientX, e\.clientY\)/);
	assert.match(pad, /stepPadDrag\(session/);
	assert.match(pad, /dot = \{ x: 0, y: 0 \}/, 'the dot no longer springs back to centre');
	assert.doesNotMatch(pad, /tabindex="0"/, 'the pad must never take keyboard focus');
	for (const cls of ['gp-axis', 'gp-diag', 'gp-half', 'gp-tick'])
		assert.ok(pad.includes(cls), `${cls} lines are gone`);
	assert.match(pad, /TICKS = \[0\.25, 0\.5, 0\.75, 1\]/);
	const mixer = readFileSync(`${MIXER}/../Mixer.svelte`, 'utf8');
	assert.match(mixer, /<GroupPad \/>/);
});

// requirement: MIXGRP-05
test('closing the group pad mid-drag ends the drag, so a later move cannot resume it (Sol P1, PR #6829)', () => {
	// Esc or X clears the selection and removes the pad's SVG before any
	// pointerup, while this copy stays mounted with the old session.
	const pad = readFileSync(`${MIXER}/GroupPad.svelte`, 'utf8');
	const end = pad.match(/function endDrag\(\): void \{([\s\S]*?)\n\t\}/);
	assert.ok(end, 'GroupPad has no endDrag');
	for (const reset of ['tracker = null;', 'session = null;', 'dot = { x: 0, y: 0 };']) {
		assert.ok(end[1].includes(reset), `endDrag does not reset: ${reset}`);
	}
	assert.match(
		pad,
		/\$effect\(\(\) => \{\s*if \(!open\) endDrag\(\);\s*\}\);/,
		'closing the pad keeps the drag session'
	);
	assert.match(
		pad,
		/onlostpointercapture=\{endDrag\}/,
		'a lost pointer capture does not cancel the drag'
	);
	// Control (the overshoot): a move still drives the open pad's session.
	assert.match(
		pad,
		/if \(tracker === null \|\| session === null\) return;\n\t\tconst step = trackDragMove/
	);
});

test('the group pad stays reachable while settings is open, on any route (Sol P1, PR #6829)', () => {
	// The drag tester's controls can form a group, but the only pad lived in
	// the Mixer: absent on routes without one, and under the settings
	// backdrop on /performance. Settings mounts its own copy inside its
	// backdrop layer, and exactly one copy shows at a time.
	const pad = readFileSync(`${MIXER}/GroupPad.svelte`, 'utf8');
	assert.match(
		pad,
		/const open = \$derived\(knobUi\.selectedIds\.length >= 2 && isSettingsOpen\(\) === inSettings\);/
	);
	const overlay = readFileSync(`${MIXER}/../../settings/SettingsOverlay.svelte`, 'utf8');
	const backdrop = overlay.indexOf('class="so-backdrop"');
	const mount = overlay.indexOf('<GroupPad inSettings />');
	assert.ok(
		backdrop >= 0 && mount > backdrop,
		'SettingsOverlay does not mount the pad inside its backdrop layer'
	);
	assert.ok(
		mount > overlay.lastIndexOf('</section>'),
		'the pad must sit outside the transformed panel, or position:fixed pins it to the panel'
	);
});

// ============================================ drag tester isolation (#6829 P1)

test('a drag-tester control never joins a live selection, so a tester drag cannot move a live control', () => {
	const live = addDial('1:low', 0.5);
	const tester = addDial('test-a:knob', 0.5);
	const testerB = addDial('test-b:knob', 0.5);
	knobs.shiftClickKnob(live);
	assert.ok(knobs.shiftClickKnob(tester), 'the tester click was refused instead of starting fresh');
	assert.deepEqual(
		[...knobs.knobUi.selectedIds],
		[tester],
		'a live control stayed grouped with the tester'
	);
	const start = knobs.beginSelectionDrag(tester);
	assert.equal(start, null, 'a lone tester control started a group drag');
	// Control: two tester controls still group and drag together.
	knobs.shiftClickKnob(testerB);
	const group = knobs.beginSelectionDrag(tester);
	assert.deepEqual([...group.ids].sort(), [tester, testerB].sort());
	knobs.stepSelectionDrag(group, 0.1, false);
	assert.equal(valueOf(live), 0.5, 'the tester drag moved a live control');
	assert.ok(valueOf(testerB) > 0.5, 'the tester group did not move');
});

test('a live selection is left whole by live shift-clicks and replaced by a tester click', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	assert.deepEqual([...knobs.knobUi.selectedIds], [a, b], 'live grouping broke');
	assert.throws(() => knobs.setKnobSelection([a, 'test-a:knob']), /cannot mix/);
});

test('a tester dial never links to a live one', () => {
	const live = addDial('1:low', 0.5);
	const tester = addDial('test-a:knob', 0.5);
	knobs.altClickKnob(live);
	knobs.altClickKnob(tester);
	assert.equal(knobs.knobUi.link, null, 'a tester dial linked to a live dial');
	assert.equal(knobs.knobUi.linkPendingId, tester, 'the tester click did not arm a fresh link');
});

test('the group hint anchors to the knob copy that is on top, not the first in the document', () => {
	const src = readFileSync(
		new URL('../../src/lib/components/rb/mixer/GroupHint.svelte', import.meta.url),
		'utf8'
	);
	assert.doesNotMatch(
		src,
		/document\.querySelector\(/,
		'the hint picks the first duplicate id again'
	);
	assert.match(src, /querySelectorAll\([\s\S]*?\.find\(_onTop\)/);
	assert.match(src, /elementFromPoint\(/);
});

test('a group-pad press that ends on the settings backdrop does not close Settings (Sol P1, PR #6829)', () => {
	// The pad sits inside the backdrop, so a drag from it ending on bare
	// backdrop dispatches its click at the backdrop itself. Runs the shipped
	// handlers, with their type annotations stripped.
	const overlay = readFileSync(`${MIXER}/../../settings/SettingsOverlay.svelte`, 'utf8');
	assert.match(
		overlay,
		/role="presentation" onpointerdown=\{onBackdropDown\} onpointerup=\{onBackdropUp\} onclick=\{onBackdrop\}>/
	);
	const start = overlay.indexOf('let pressOnBackdrop = false;');
	const end = overlay.indexOf('function onSearchKeydown');
	assert.ok(start > 0 && end > start, 'the backdrop handlers moved');
	const body = overlay.slice(start, end).replace(/: (PointerEvent|MouseEvent|void)/g, '');
	let closed = 0;
	const handlers = new Function(
		'closeSettings',
		`${body}; return { onBackdropDown, onBackdropUp, onBackdrop };`
	)(() => {
		closed += 1;
	});
	const backdrop = {};
	const pad = {};
	// A drag from the pad, released over bare backdrop.
	handlers.onBackdropDown({ target: pad, currentTarget: backdrop });
	handlers.onBackdropUp({ target: backdrop, currentTarget: backdrop });
	handlers.onBackdrop({ target: backdrop, currentTarget: backdrop });
	assert.equal(closed, 0, 'a pad drag closed Settings');
	// A plain click inside the pad.
	handlers.onBackdropDown({ target: pad, currentTarget: backdrop });
	handlers.onBackdropUp({ target: pad, currentTarget: backdrop });
	handlers.onBackdrop({ target: pad, currentTarget: backdrop });
	assert.equal(closed, 0, 'a pad click closed Settings');
	// Control: a real outside click still closes it.
	handlers.onBackdropDown({ target: backdrop, currentTarget: backdrop });
	handlers.onBackdropUp({ target: backdrop, currentTarget: backdrop });
	handlers.onBackdrop({ target: backdrop, currentTarget: backdrop });
	assert.equal(closed, 1, 'an outside click no longer closes Settings');
	// A click with no press seen (the pad stopped its pointerdown) cannot close it.
	handlers.onBackdrop({ target: backdrop, currentTarget: backdrop });
	assert.equal(closed, 1, 'a stale press closed Settings');
});

test('a press on the settings backdrop released over the panel does not close Settings (Sol P1, PR #6829)', () => {
	// The real retargeting sequence: pointerdown on bare backdrop, pointerup
	// over the panel, and the browser dispatches the click at their common
	// ancestor, the backdrop. Runs the shipped handlers, types stripped.
	const overlay = readFileSync(`${MIXER}/../../settings/SettingsOverlay.svelte`, 'utf8');
	const start = overlay.indexOf('let pressOnBackdrop = false;');
	const end = overlay.indexOf('function onSearchKeydown');
	assert.ok(start > 0 && end > start, 'the backdrop handlers moved');
	const body = overlay.slice(start, end).replace(/: (PointerEvent|MouseEvent|void)/g, '');
	let closed = 0;
	const handlers = new Function(
		'closeSettings',
		`${body}; return { onBackdropDown, onBackdropUp, onBackdrop };`
	)(() => {
		closed += 1;
	});
	const backdrop = {};
	const panel = {};
	handlers.onBackdropDown({ target: backdrop, currentTarget: backdrop });
	handlers.onBackdropUp({ target: panel, currentTarget: backdrop });
	handlers.onBackdrop({ target: backdrop, currentTarget: backdrop });
	assert.equal(closed, 0, 'a backdrop press released over the panel closed Settings');
	// The next genuine outside click is not poisoned by the abandoned release.
	handlers.onBackdropDown({ target: backdrop, currentTarget: backdrop });
	handlers.onBackdropUp({ target: backdrop, currentTarget: backdrop });
	handlers.onBackdrop({ target: backdrop, currentTarget: backdrop });
	assert.equal(closed, 1, 'a real outside click after it no longer closes Settings');
	// A release left over from an earlier press does not stand in for this one's.
	handlers.onBackdropDown({ target: backdrop, currentTarget: backdrop });
	handlers.onBackdropUp({ target: backdrop, currentTarget: backdrop });
	handlers.onBackdropDown({ target: backdrop, currentTarget: backdrop });
	handlers.onBackdrop({ target: backdrop, currentTarget: backdrop });
	assert.equal(closed, 1, 'a stale release closed Settings');
});

function fireCommandedKey(partial = {}) {
	let prevented = false;
	let stopped = false;
	const event = {
		key: '5',
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		shiftKey: false,
		target: null,
		preventDefault: () => {
			prevented = true;
		},
		stopPropagation: () => {
			stopped = true;
		},
		...partial
	};
	globalThis.__listeners.get('keydown:capture')(event);
	return { prevented, stopped };
}

test('digit 5 on selected 4:low plans a glide without changing the dial value', () => {
	const a = addDial('4:low', 0.9);
	knobs.shiftClickKnob(a);
	const recorder = [];
	knobs._setCommandedGlideStarterForTests((moves) => recorder.push(...moves));
	const { prevented } = fireCommandedKey({ key: '5' });
	assert.ok(prevented);
	assert.deepEqual(recorder, [{ controlId: '4:low', from: 0.9, to: 0.5, beats: 4 }]);
	assert.equal(valueOf(a), 0.9);
});

test('Cmd+2 on selected 4:low jumps immediately without calling the glide starter', () => {
	const a = addDial('4:low', 0.9);
	knobs.shiftClickKnob(a);
	const recorder = [];
	knobs._setCommandedGlideStarterForTests((moves) => recorder.push(...moves));
	fireCommandedKey({ key: '2', metaKey: true });
	assert.equal(recorder.length, 0);
	assert.ok(near(valueOf(a), 0.125));
});

test('bare 2 with nothing selected does not stop propagation or start a glide', () => {
	const a = addDial('4:low', 0.5);
	const recorder = [];
	knobs._setCommandedGlideStarterForTests((moves) => recorder.push(...moves));
	const { stopped } = fireCommandedKey({ key: '2' });
	assert.equal(recorder.length, 0);
	assert.equal(valueOf(a), 0.5);
	assert.ok(!stopped);
});

test('digit 5 on two shift-selected lows plans two glides', () => {
	const a = addDial('1:low', 0.1);
	const b = addDial('2:low', 0.9);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	const recorder = [];
	knobs._setCommandedGlideStarterForTests((moves) => recorder.push(...moves));
	fireCommandedKey({ key: '5' });
	assert.equal(recorder.length, 2);
	assert.equal(recorder[0].to, 0.5);
	assert.equal(recorder[1].to, 0.5);
});

test('a selected id with no mounted registry entry is ignored for commanded keys', () => {
	const a = addDial('1:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.knobUi.selectedIds = ['1:low', '9:low'];
	const recorder = [];
	knobs._setCommandedGlideStarterForTests((moves) => recorder.push(...moves));
	fireCommandedKey({ key: '5' });
	assert.equal(recorder.length, 1);
	assert.equal(recorder[0].controlId, '1:low');
});

test('commandedKeyHost selectedIds matches the bridge filtered to mounted ids', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	knobs.knobUi.selectedIds = ['1:low', '2:low', '9:low'];
	const bridge = globalThis.window.__mdtKnobSelection.get();
	const hostIds = knobs.commandedKeyHost().selectedIds();
	assert.deepEqual(
		hostIds,
		bridge.filter((id) => id !== '9:low')
	);
});

test('__mdtKnobSelection.command runs dial position and jump on the selection', () => {
	const bridge = globalThis.window.__mdtKnobSelection;
	const a = addDial('1:low', 0.2);
	knobs.shiftClickKnob(a);
	bridge.command('dial.jump.2');
	assert.equal(knobs.readKnobValue(a), 0.125);
	const b = addDial('2:low', 0.1);
	knobs.setKnobSelection([a, b]);
	const recorder = [];
	knobs._setCommandedGlideStarterForTests((moves) => recorder.push(...moves));
	bridge.command('dial.position.5');
	assert.equal(recorder.length, 2);
	assert.ok(recorder.every((move) => move.to === 0.5 && move.beats === 4));
	knobs.clearKnobSelection();
	assert.throws(() => bridge.command('dial.jump.2'), /no dial or fader is selected/);
	assert.throws(() => bridge.command('preset.run.bass-swap-8'), TypeError);
	assert.throws(() => bridge.command('nope'), TypeError);
});

// ============================================ onKnobHandInput (#9454)

test('onKnobHandInput: hand paths report ids; setter path is silent', () => {
	const seen = [];
	const off = knobs.onKnobHandInput((ids) => seen.push([...ids]));
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.setKnobFromDrag(a, 0.5, 0.6, null, null);
	assert.ok(seen.some((batch) => batch.includes(a)));
	seen.length = 0;
	knobs.beginSelectionDrag(a);
	assert.ok(seen.some((batch) => batch.includes(a)));
	seen.length = 0;
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	knobs.beginSelectionDrag(a);
	assert.ok(seen.some((batch) => batch.includes(a) && batch.includes(b)));
	seen.length = 0;
	const padStart = knobs.beginPadDrag();
	assert.ok(padStart);
	assert.ok(seen.length > 0);
	seen.length = 0;
	knobs.stepSelectionDrag(padStart, 0.05, false);
	assert.ok(seen.some((batch) => batch.includes(a)));
	seen.length = 0;
	knobs.stepPadDrag(padStart, 0.05, -0.05);
	assert.ok(seen.length > 0);
	seen.length = 0;
	knobs.nudgeKnobOrSelection(a, 0.02);
	assert.ok(seen.some((batch) => batch.includes(a)));
	seen.length = 0;
	knobs.nudgeSelection(0.02);
	assert.ok(seen.length > 0);
	seen.length = 0;
	const bridge = globalThis.window.__mdtKnobSelection;
	bridge.nudge(0.01);
	assert.ok(seen.length > 0);
	seen.length = 0;
	bridge.drag(0, knobs.KNOB_CFG.dragVerticalPx * 0.05);
	assert.ok(seen.some((batch) => batch.includes(a) || batch.includes(b)));
	seen.length = 0;
	knobs.setKnobAbsolute(a, 0.7);
	knobs.nudgeKnob(a, 0.01);
	assert.equal(seen.length, 0);
	off();
	knobs.nudgeKnobOrSelection(a, 0.01);
	assert.equal(seen.length, 0);
});

test('setKnobFromBaseline and silent setters do not fire hand input', () => {
	const seen = [];
	const off = knobs.onKnobHandInput((ids) => seen.push([...ids]));
	const a = addDial('1:low', 0.5);
	knobs.setKnobAbsolute(a, 0.7);
	knobs.nudgeKnob(a, 0.01);
	knobs.setKnobFromBaseline(a, 0.5, 0.6, null, null);
	assert.equal(seen.length, 0);
	off();
});

test('setKnobFromDrag matches setKnobFromBaseline and fires hand input', () => {
	const a = addDial('1:low', 0.5);
	knobs.setKnobFromDrag(a, 0.5, 0.65, null, null);
	assert.equal(knobs.readKnobValue(a), 0.65);
	const seen = [];
	knobs.onKnobHandInput((ids) => seen.push([...ids]));
	knobs.setKnobFromDrag(a, 0.65, 0.7, null, null);
	assert.ok(seen.length > 0);
	const baseline = knobs.setKnobFromBaseline(a, 0.7, 0.75, null, null);
	assert.deepEqual(baseline, { primary: 0.75, secondary: null });
});

test('setKnobFromBaseline returns null when unmounted and linked pair when linked', () => {
	assert.equal(knobs.setKnobFromBaseline('9:low', 0, 1, null, null), null);
	const a = addDial('1:low', 0.5);
	assert.deepEqual(knobs.setKnobFromBaseline(a, 0.5, 0.6, null, null), {
		primary: 0.6,
		secondary: null
	});
	const b = addDial('2:low', 0.5);
	knobs.altClickKnob(a);
	knobs.altClickKnob(b);
	const linked = knobs.setKnobFromBaseline(a, 0.5, 0.7, b, 0.5);
	const expected = knobs.applyLinkedDelta(0.5, 0.5, 0.2);
	assert.ok(Math.abs(linked.primary - expected.primary) < 1e-9);
	assert.ok(Math.abs(linked.secondary - expected.secondary) < 1e-9);
});

test('noteKnobHandInput expands the Alt-link partner', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.altClickKnob(a);
	knobs.altClickKnob(b);
	const seen = [];
	knobs.onKnobHandInput((ids) => seen.push([...ids]));
	knobs.noteKnobHandInput(a);
	assert.ok(seen.some((batch) => batch.includes(a) && batch.includes(b)));
});

// requirement: MIXUX-10355 (issue #9547)
test('the pickup sentence stays in the accessibility tree but is visually hidden in scope', () => {
	const knob = readFileSync(`${MIXER}/Knob.svelte`, 'utf8');
	const style = knob.slice(knob.lastIndexOf('<style>'));
	assert.match(
		knob,
		/<span class="sr-only">Hardware at \{\(takeoverGhost\.value \* 100\)\.toFixed\(0\)\}%/
	);
	const rule = style.match(/\.sr-only \{([^}]*)\}/);
	assert.ok(rule, 'Knob.svelte scopes its own .sr-only rule');
	for (const decl of [
		'position: absolute',
		'width: 1px',
		'height: 1px',
		'overflow: hidden',
		'clip: rect(0 0 0 0)',
		'white-space: nowrap'
	]) {
		assert.ok(rule[1].includes(decl), `.sr-only carries ${decl}`);
	}
});

test('the pickup hint is hover text on the live knob title, and the ghost marker stays', () => {
	const knob = readFileSync(`${MIXER}/Knob.svelte`, 'utf8');
	assert.match(
		knob,
		/const takeoverHint = \$derived\(\s*takeoverGhost === null\s*\? ''\s*: ` - hardware at \$\{/
	);
	assert.match(knob, /: `\$\{label\}\$\{takeoverHint\}\$\{selected \?/);
	assert.match(knob, /class:takeover-armed=\{takeoverGhost !== null\}/);
	assert.match(knob, /pickup-ghost-indicator/);
});
