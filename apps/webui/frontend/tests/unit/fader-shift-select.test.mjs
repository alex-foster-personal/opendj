import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Channel LEVEL faders join the knob selection (MIXUX-13) under <deck>:fader.
// Shift-click toggles membership and does not move the fader. A drag or the
// global wheel then moves every selected control through the existing
// two-axis / common-mode baselines, each clamped on its own 0..1 range.
//
// The strip's click handler is MIXUX-08 deck select. A fader shift-click must
// not reach it. pointerdown stopPropagation does not stop the later click, so
// the fader also stops that click, and only when shift is held.

const MIXER = fileURLToPath(new URL('../../src/lib/components/rb/mixer', import.meta.url));
const RB = fileURLToPath(new URL('../../src/lib/components/rb', import.meta.url));

let knobs;
let dials;

class FakeHTMLElement {
	constructor(tagName, { isContentEditable = false } = {}) {
		this.tagName = tagName;
		this.isContentEditable = isContentEditable;
	}
}

before(async () => {
	const listeners = new Map();
	const store = new Map();
	globalThis.window = {
		addEventListener: (type, fn) => listeners.set(type, fn),
		removeEventListener: (type) => listeners.delete(type),
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	globalThis.HTMLElement = FakeHTMLElement;
	globalThis.__faderSelectListeners = listeners;
	knobs = await loadTypeScriptModule('src/lib/rb/knob-control.svelte.ts');
});

beforeEach(() => {
	knobs.clearKnobLink();
	knobs.knobUi.selectedIds = [];
	knobs.knobUi.hoveredId = null;
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
	return id;
}

const valueOf = (id) => dials.get(id).value;
const near = (a, b) => Math.abs(a - b) < 1e-9;

function fakeTarget(matches) {
	return {
		closest: (sel) =>
			matches.some((m) => sel.split(',').map((part) => part.trim()).includes(m)) ? {} : null
	};
}

function wheelEvent(deltaY) {
	let prevented = false;
	return {
		deltaY,
		target: null,
		deltaMode: 0,
		wheelDeltaY: deltaY > 0 ? -120 : 120,
		preventDefault: () => {
			prevented = true;
		},
		wasPrevented: () => prevented
	};
}

test('fader ids are <deck>:fader and share their deck channel with that deck EQs', () => {
	assert.equal(knobs.faderControlId(1), '1:fader');
	assert.equal(knobs.faderControlId(4), '4:fader');
	assert.equal(knobs.knobGroupOf('1:fader'), knobs.knobGroupOf(knobs.knobId(1, 'low')));
	assert.notEqual(knobs.knobGroupOf('1:fader'), knobs.knobGroupOf(knobs.knobId(2, 'low')));
});

test('shift-click on two faders selects both and does not move them', () => {
	const a = addDial(knobs.faderControlId(1), 0.4);
	const b = addDial(knobs.faderControlId(2), 0.7);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	assert.deepEqual([...knobs.selectedKnobIds()], [a, b]);
	assert.equal(valueOf(a), 0.4);
	assert.equal(valueOf(b), 0.7);
	knobs.shiftClickKnob(a);
	assert.deepEqual([...knobs.selectedKnobIds()], [b], 'a second shift-click did not toggle the fader off');
	assert.equal(valueOf(a), 0.4);
});

test('shift-click on a fader and an EQ knob selects both', () => {
	const fader = addDial(knobs.faderControlId(1), 0.55);
	const eq = addDial(knobs.knobId(2, 'low'), 0.33);
	knobs.shiftClickKnob(fader);
	knobs.shiftClickKnob(eq);
	assert.deepEqual([...knobs.selectedKnobIds()], [fader, eq]);
	assert.equal(valueOf(fader), 0.55);
	assert.equal(valueOf(eq), 0.33);
});

test('a drag on one selected control moves every selected fader and knob, each clamped to 0..1', () => {
	// The dragged control leads (R2); the rest move the same way. The first
	// one starts near the top so it clamps while the rest travel the full delta.
	const first = addDial(knobs.faderControlId(1), 0.95);
	const dragged = addDial(knobs.faderControlId(2), 0.2);
	const eq = addDial(knobs.knobId(2, 'high'), 0.1);
	knobs.shiftClickKnob(first);
	knobs.shiftClickKnob(dragged);
	knobs.shiftClickKnob(eq);
	const start = knobs.beginSelectionDrag(dragged);
	assert.ok(start !== null, 'dragging a selected fader did not start a selection drag');
	knobs.stepSelectionDrag(start, 0.2, false);
	assert.equal(valueOf(first), 1, 'the fader near the top was not clamped to 1');
	assert.ok(near(valueOf(dragged), 0.4), `the dragged fader did not move by the full delta (${valueOf(dragged)})`);
	assert.ok(near(valueOf(eq), 0.3), `the EQ knob did not move by the full delta (${valueOf(eq)})`);
});

test('control: a drag does not move a fader that was left out of the selection', () => {
	// If a multi-move swept every registered fader, this fails. The companion
	// test above fails if a selected fader does not move.
	const a = addDial(knobs.knobId(1, 'low'), 0.4);
	const b = addDial(knobs.knobId(2, 'low'), 0.4);
	const fader = addDial(knobs.faderControlId(1), 0.4);
	knobs.shiftClickKnob(a);
	knobs.shiftClickKnob(b);
	const start = knobs.beginSelectionDrag(a);
	assert.ok(start !== null);
	knobs.stepSelectionDrag(start, 0.2, false);
	assert.ok(near(valueOf(a), 0.6), 'the lead knob did not move');
	assert.ok(near(valueOf(b), 0.6), 'the other selected knob did not move');
	assert.equal(valueOf(fader), 0.4, 'a fader outside the selection moved with the drag');
});

test('a plain click on empty space clears a fader selection; a click on the fader does not', () => {
	const fader = addDial(knobs.faderControlId(1), 0.5);
	const eq = addDial(knobs.knobId(1, 'high'), 0.5);
	knobs.shiftClickKnob(fader);
	knobs.shiftClickKnob(eq);
	const down = globalThis.__faderSelectListeners.get('pointerdown');
	assert.equal(typeof down, 'function');
	down({ button: 0, target: fakeTarget(['[data-knob-id]']) });
	assert.deepEqual([...knobs.selectedKnobIds()], [fader, eq], 'a click on the fader cleared the selection');
	down({ button: 0, target: fakeTarget(['[role="slider"]']) });
	assert.deepEqual([...knobs.selectedKnobIds()], [fader, eq], 'a click on a slider cleared the selection');
	down({ button: 0, target: fakeTarget([]) });
	assert.deepEqual([...knobs.selectedKnobIds()], [], 'a click on empty space left the selection');
});

test('the global wheel moves a mixed fader and EQ selection together', () => {
	const fader = addDial(knobs.faderControlId(3), 0.5);
	const eq = addDial(knobs.knobId(3, 'mid'), 0.5);
	knobs.shiftClickKnob(fader);
	knobs.shiftClickKnob(eq);
	const onWheel = globalThis.__faderSelectListeners.get('wheel');
	const event = wheelEvent(-100);
	onWheel(event);
	assert.ok(event.wasPrevented());
	const step = knobs.KNOB_CFG.scrollStep;
	assert.ok(near(valueOf(fader), 0.5 + step), `fader ended at ${valueOf(fader)}`);
	assert.ok(near(valueOf(eq), 0.5 + step), `EQ ended at ${valueOf(eq)}`);
});

test('the existing agent bridge drives a mounted fader id', () => {
	const bridge = globalThis.window.__mdtKnobSelection;
	assert.ok(bridge, 'window.__mdtKnobSelection is not installed');
	const fader = addDial(knobs.faderControlId(2), 0.5);
	const eq = addDial(knobs.knobId(1, 'filter'), 0.5);
	bridge.set([fader, eq]);
	assert.deepEqual(bridge.get(), [fader, eq]);
	bridge.nudge(0.1);
	assert.ok(near(bridge.value(fader), 0.6) && near(bridge.value(eq), 0.6));
	bridge.clear();
	assert.deepEqual(bridge.get(), []);
});

test('VFader registers the channel fader and stops only a shift-click from selecting the deck', () => {
	const source = readFileSync(`${MIXER}/VFader.svelte`, 'utf8');
	const strip = readFileSync(`${MIXER}/ChannelStrip.svelte`, 'utf8');
	const mixer = readFileSync(`${RB}/Mixer.svelte`, 'utf8');

	assert.match(source, /from '\$lib\/rb\/knob-control\.svelte'/);
	for (const fn of [
		'faderControlId',
		'registerKnob',
		'unregisterKnob',
		'shiftClickKnob',
		'beginSelectionDrag',
		'stepSelectionDrag',
		'trackDragMove',
		'nudgeKnobOrSelection'
	]) {
		assert.ok(source.includes(`${fn}(`), `VFader.svelte does not call ${fn}`);
	}
	assert.match(source, /setValue: \(next\) => onchange\(next\)/);
	assert.match(source, /data-knob-id=\{controlId\}/);
	assert.match(source, /class:fader-selected=\{selected\}/);
	assert.match(source, /onclick=\{handleClick\}/);
	assert.doesNotMatch(source, /clickSelect/);

	const click = source.slice(source.indexOf('function handleClick('), source.indexOf('function handlePointerDown('));
	assert.match(click, /if \(!shiftSelect\.click\(e\.shiftKey\)\) return;/);
	assert.match(click, /e\.stopPropagation\(\)/);

	const pointer = source.slice(
		source.indexOf('function handlePointerDown('),
		source.indexOf('function handlePointerMove(')
	);
	assert.match(pointer, /e\.shiftKey/);
	assert.match(pointer, /e\.stopPropagation\(\)/);
	assert.match(pointer, /shiftClickKnob\(controlId\)/);
	const shiftAt = pointer.indexOf('shiftClickKnob');
	const changeAt = pointer.indexOf('onchange(');
	assert.ok(shiftAt !== -1 && changeAt > shiftAt, 'shift-click moves the fader before it returns');
	assert.ok(
		pointer.indexOf('return;', shiftAt) < changeAt,
		'shift-click does not return before onchange'
	);

	assert.match(strip, /function handleStripClick\(event: MouseEvent\)/);
	assert.match(strip, /clickSelect\(deckId, event\.shiftKey\)/);
	assert.match(strip, /onclick=\{handleStripClick\}/);
	assert.match(strip, /onchange=\{onfader\}/);
	assert.match(mixer, /function handleFader\(deck: DeckId, value: number\)/);
	assert.match(mixer, /type: 'fader'/);
	assert.match(mixer, /onfader=\{\(v\) => handleFader\(deck, v\)\}/);
});

// MIXGRP-01: Shift let go between pointerdown and pointerup must not turn the
// release into a plain click that selects the deck.
const { createShiftSelectPress } = await loadTypeScriptModule('src/lib/rb/shift-select-press.ts');

test('MIXGRP-01: a shift-press stops its click even when Shift is up by the release', () => {
	const guard = createShiftSelectPress();
	guard.press(true);
	assert.equal(guard.click(false), true, 'the release click reached the strip and selected the deck');
	// The press is consumed: a click with no press of its own (keyboard
	// activation) and the next plain press and click select the deck again.
	assert.equal(guard.click(false), false, 'a later keyboard click was swallowed by an old press');
	guard.press(false);
	assert.equal(guard.click(false), false, 'a plain click after a shift-press was swallowed');
	// Control: a click that carries Shift itself is still stopped.
	assert.equal(guard.click(true), true);
	// A shift-press released off the control (no click) does not swallow the next plain click.
	guard.press(true);
	guard.press(false);
	assert.equal(guard.click(false), false);
});

test('MIXGRP-01: knob and fader record every press before the shift branch', () => {
	for (const file of ['Knob.svelte', 'VFader.svelte']) {
		const src = readFileSync(new URL(`../../src/lib/components/rb/mixer/${file}`, import.meta.url), 'utf8');
		const pointer = src.slice(src.indexOf('function handlePointerDown('));
		const pressAt = pointer.indexOf('shiftSelect.press(e.shiftKey)');
		assert.ok(pressAt !== -1 && pressAt < pointer.indexOf('if (e.shiftKey)'), `${file} records the press after the shift branch`);
		assert.match(src, /if \(!shiftSelect\.click\(e\.shiftKey\)\) return;/, `${file} click ignores the press`);
	}
});

test('MIXGRP-01: knob and fader ignore a non-primary press before it can toggle or drag the selection', () => {
	// Sol P2, PR #6829: a shift-right-click toggled the fader in the group, and
	// a right or middle drag moved the group under the context menu.
	for (const file of ['Knob.svelte', 'VFader.svelte']) {
		const src = readFileSync(new URL(`../../src/lib/components/rb/mixer/${file}`, import.meta.url), 'utf8');
		const pointer = src.slice(src.indexOf('function handlePointerDown('));
		const guardAt = pointer.search(/if \([^)]*e\.button !== 0\) return;/);
		assert.ok(guardAt !== -1, `${file} has no primary-button guard`);
		assert.ok(guardAt < pointer.indexOf('shiftSelect.press(e.shiftKey)'), `${file} records a non-primary press`);
	}
});
