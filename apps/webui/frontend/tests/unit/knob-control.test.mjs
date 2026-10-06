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
// - if the global wheel stops scaling by input kind then the shift-selected
//   dial is hypersensitive on a trackpad while every use:wheelAdjust control
//   is calm, i.e. the "global" sensitivity fix has a hole in it
// - if horizontal drag is not much less sensitive than vertical then fine adjust
//   is impossible
// - if Knob.svelte stops importing knob-control then the module is dead code
//   again and a refactor deletes the headline feature

const MIXER = fileURLToPath(new URL('../../src/lib/components/rb/mixer', import.meta.url));

let knobs;
/** wheel-adjust, for WHEEL_TRACKPAD_EVENTS_PER_DETENT only. */
let wheel;
/** Registered fake dials by id, each holding a real settable value. */
let dials;
let wheelHandler;

/** The wheel guard tests `target instanceof HTMLElement`, so node needs one. */
class FakeHTMLElement {
	constructor(tagName, { isContentEditable = false } = {}) {
		this.tagName = tagName;
		this.isContentEditable = isContentEditable;
	}
}

before(async () => {
	// The module binds a window wheel listener on the first registerKnob().
	const listeners = new Map();
	const store = new Map();
	globalThis.window = {
		addEventListener: (type, fn) => listeners.set(type, fn),
		removeEventListener: (type) => listeners.delete(type),
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
	globalThis.__listeners = listeners;
});

beforeEach(() => {
	knobs.clearKnobLink();
	knobs.knobUi.selectedIds = [];
	knobs.knobUi.hoveredId = null;
	// _registry is module-level state, not reset by the clears above - without
	// this, a dial id reused across test cases (every case below reuses
	// '1:low'/'2:low') carries a leftover registration stack into the next
	// test and silently changes its unregisterKnob() behavior.
	knobs._resetKnobRegistryForTests();
	dials = new Map();
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
	wheelHandler = globalThis.__listeners.get('wheel');
	return id;
}

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

	assert.equal(knobs.linkedPartnerId(a), b, 'the mixer knob underneath lost its link when only the shadow unregistered');
	knobs.nudgeKnob(a, 0.1);
	assert.equal(valueOf(b), 0.4, 'the link must still drive the partner after the shadow registration is gone');
});

// ============================================ selection + the global wheel

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
	return { closest: (sel) => (matches.some((m) => sel.split(',').map((x) => x.trim()).includes(m)) ? {} : null) };
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
	assert.ok(near(valueOf(a), 0.4) && near(valueOf(b), 0.4), 'the selection did not move in common mode');
	knobs.nudgeKnobOrSelection(c, 0.1);
	assert.ok(near(valueOf(c), 0.6));
	assert.ok(near(valueOf(a), 0.4), 'turning an unselected dial dragged the selection along');
});

test('two-axis drag: up+right is all up, up+left is the first up and the others down', () => {
	const sel = ['1:low', '2:low'];
	const base = { '1:low': 0.5, '2:low': 0.5 };
	const px = knobs.KNOB_CFG.dragVerticalPx * 0.1;
	const upRight = knobs.twoAxisDragTargets(sel, base, px, px);
	assert.ok(near(upRight['1:low'], 0.6) && near(upRight['2:low'], 0.6), `up+right ${JSON.stringify(upRight)}`);
	const upLeft = knobs.twoAxisDragTargets(sel, base, -px, px);
	assert.ok(near(upLeft['1:low'], 0.6), 'up+left must still raise the first-selected deck');
	assert.ok(near(upLeft['2:low'], 0.4), 'up+left must lower the other deck');
	const upOnly = knobs.twoAxisDragTargets(sel, base, 0, px);
	assert.ok(near(upOnly['2:low'], 0.5), 'pure vertical moved the second deck - the axes are not independent');
	const rightOnly = knobs.twoAxisDragTargets(sel, base, px, 0);
	assert.ok(near(rightOnly['1:low'], 0.5), 'pure horizontal moved the first-selected deck');
	assert.ok(near(rightOnly['2:low'], 0.6), 'right must be up for the other deck');
});

test('two-axis drag: vertical drives every selected dial on the first-selected deck', () => {
	const sel = ['1:low', '2:low', '1:high'];
	const base = { '1:low': 0.5, '2:low': 0.5, '1:high': 0.2 };
	const px = knobs.KNOB_CFG.dragVerticalPx * 0.1;
	const out = knobs.twoAxisDragTargets(sel, base, 0, px);
	assert.ok(near(out['1:high'], 0.3), 'a second dial on the lead deck did not follow the vertical axis');
	assert.ok(near(out['2:low'], 0.5));
	// Order decides the lead deck, not the id.
	const flipped = knobs.twoAxisDragTargets(['2:low', '1:low'], base, 0, px);
	assert.ok(near(flipped['2:low'], 0.6) && near(flipped['1:low'], 0.5), 'the lead deck is not the first-selected one');
});

test('two-axis drag clamps, and one selected dial keeps horizontal fine adjust', () => {
	const big = knobs.KNOB_CFG.dragVerticalPx * 2;
	const out = knobs.twoAxisDragTargets(['1:low', '2:low'], { '1:low': 0.9, '2:low': 0.1 }, -big, big);
	assert.equal(out['1:low'], 1);
	assert.equal(out['2:low'], 0);
	assert.equal(knobs.twoAxisDragTargets(['1:low'], { '1:low': 0.5 }, 50, 0), null,
		'a single selected dial must not enter two-axis mode');
	const fine = knobs.singleDragDelta(100, 0);
	const coarse = knobs.singleDragDelta(0, 100);
	assert.ok(fine > 0 && fine * 3 <= coarse, `horizontal ${fine} is not fine adjust next to vertical ${coarse}`);
});

test('a selection drag starts only on a selected dial of a 2+ set and never accumulates', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	const c = addDial('3:low', 0.5);
	knobs.shiftClickKnob(a);
	assert.equal(knobs.beginSelectionDrag(a), null, 'one selected dial must keep the single-dial drag');
	knobs.shiftClickKnob(b);
	assert.equal(knobs.beginSelectionDrag(c), null, 'dragging an unselected dial must not move the selection');
	const start = knobs.beginSelectionDrag(b);
	assert.ok(start !== null);
	const px = knobs.KNOB_CFG.dragVerticalPx;
	for (const f of [0.02, 0.05, 0.08, 0.1]) knobs.applySelectionDrag(start, f * px, f * px);
	assert.ok(near(valueOf(a), 0.6) && near(valueOf(b), 0.6), `slow drag ended at ${valueOf(a)}, ${valueOf(b)}`);
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
	const down = globalThis.__listeners.get('pointerdown');
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
	assert.deepEqual([...knobs.selectedKnobIds()], [a, b], 'a re-render dropped the dial from the selection');
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
	assert.ok(near(bridge.value(a), 0.6) && near(bridge.value(b), 0.6), 'bridge nudge is not common mode');
	const px = knobs.KNOB_CFG.dragVerticalPx * 0.1;
	bridge.drag(-px, px);
	assert.ok(near(valueOf(a), 0.7) && near(valueOf(b), 0.5), `bridge up+left drag gave ${valueOf(a)}, ${valueOf(b)}`);
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
		['applySelectionDrag', 'the two-axis drag is not wired'],
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
	assert.equal(knobs.pointerTravelIsDrag(0, 0), false,
		'if a pointermove with no travel counts as a drag then a still click never steps MIX - broken');
	assert.equal(knobs.pointerTravelIsDrag(1, -1), false,
		'if one pixel of mouse or touch jitter counts as a drag then the 1/3 step is lost - broken');
	assert.equal(knobs.pointerTravelIsDrag(slop, 0), false);
	assert.equal(knobs.pointerTravelIsDrag(0, slop + 1), true,
		'if travel past the slop is still a click then a short drag also fires the step - broken');
	assert.equal(knobs.pointerTravelIsDrag(-(slop + 1), 0), true);
});

test('the Knob classifies drags by travel and only steps on a primary click', () => {
	const knob = readFileSync(`${MIXER}/Knob.svelte`, 'utf8');
	assert.match(knob, /pointerTravelIsDrag\(/,
		'if Knob.svelte marks any pointermove as a drag then jitter swallows the MIX click - broken');
	assert.match(knob, /onsingleclick !== undefined && insideSlop/,
		'if the click slop applies to every knob then 1-4 px fine adjustments on EQ and GAIN are discarded - broken');
	assert.match(knob, /e\.button !== 0/,
		'if a right or middle click starts a gesture then a context-menu click steps MIX - broken');
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
	assert.match(source, /nudge: \(delta\) => nudgeKnobOrSelection\(knobId, delta\)/,
		'if the wheel derives its delta from the clamped value then a dial at 0 or 1 freezes the selection');
});

test('control: with no selection, a dial own wheel step moves only that dial, clamped as before', () => {
	const a = addDial('1:low', 1);
	const b = addDial('2:low', 0.5);
	knobs.nudgeKnobOrSelection(a, 0.028);
	knobs.nudgeKnobOrSelection(b, -0.028);
	assert.equal(valueOf(a), 1, 'a single dial at the top end left the 0..1 domain');
	assert.ok(near(valueOf(b), 0.472), 'a single unselected dial no longer moves by exactly one step');
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
	assert.ok(near(valueOf(a), 0.6 + 0.1 * knobs.KNOB_CFG.linkStagger), 'the rising linked dial lost its stagger');
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
	assert.ok(near(valueOf(a), 0.6) && near(valueOf(p), 0.6),
		`a selected linked pair did not move together (${valueOf(a)}, ${valueOf(p)}) - the link overrode the selection`);
	const start = knobs.beginSelectionDrag(a);
	const px = knobs.KNOB_CFG.dragVerticalPx * 0.1;
	knobs.applySelectionDrag(start, -px, px);
	assert.ok(near(valueOf(p), 0.7) && near(valueOf(a), 0.5),
		`the two-axis drag on a selected linked pair gave ${valueOf(p)}, ${valueOf(a)}`);
});

test('a two-axis drag keeps the Alt link too, from the pointer-down baselines', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:high', 0.5);
	const p = addDial('3:low', 0.5);
	knobs.altClickKnob(a);
	knobs.altClickKnob(p);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	const start = knobs.beginSelectionDrag(a);
	const px = knobs.KNOB_CFG.dragVerticalPx * 0.1;
	for (const f of [0.3, 0.6, 1]) knobs.applySelectionDrag(start, 0, f * px);
	assert.ok(near(valueOf(p), 0.4), `the partner did not track inversely without accumulating (${valueOf(p)})`);
	assert.ok(near(valueOf(b), 0.5), 'pure vertical moved the other deck');
});

test('control: with no link, group moves are plain common mode', () => {
	const out = knobs.applyLinkToTargets(['1:low', '2:low'], { '1:low': 0.5, '2:low': 0.5 }, { '1:low': 0.6, '2:low': 0.6 }, null);
	assert.deepEqual(out, { '1:low': 0.6, '2:low': 0.6 });
});
