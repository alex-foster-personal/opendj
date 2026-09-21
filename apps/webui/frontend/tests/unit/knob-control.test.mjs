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
	knobs.knobUi.selectedId = null;
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

test('unregistering half a pair clears the link rather than leaving a dangling partner', () => {
	const a = addDial('1:low', 0.5);
	const b = addDial('2:low', 0.5);
	knobs.altClickKnob(a);
	knobs.altClickKnob(b);
	knobs.unregisterKnob(b);

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
	assert.equal(knobs.knobUi.selectedId, null);
	wheelHandler(wheelEvent(-100));
	assert.equal(valueOf(a), 0.5, 'a deselected dial still answers the global wheel');
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
		['altClickKnob', 'alt+click no longer links a pair'],
		['setKnobFromDrag', 'a drag bypasses the link maths, so the partner never moves'],
		['setKnobAbsolute', 'the wheel or keyboard bypasses the link maths'],
		['unregisterKnob', 'an unmounted dial stays in the registry forever']
	]) {
		assert.ok(source.includes(`${fn}(`), `Knob.svelte does not call ${fn}: ${why}`);
	}
	assert.ok(
		source.includes('KNOB_CFG.dragVerticalPx') && source.includes('KNOB_CFG.dragHorizontalPx'),
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
