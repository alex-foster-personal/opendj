// requirement: KEYB-14370
/**
 * #14324: the arrows adjust the selected crossfader, pitch and jog leaves (selector-activate.ts).
 *
 * The real router, selector session, command registry, leaf host and knob registry run together;
 * only the DOM (the node snapshot, the leaf elements and their controls) is a fixture.
 *
 * Regression lines:
 * - if ArrowUp on a selected pitch or crossfader leaf does not reach its slider as one keydown then broken
 * - if the slider's forwarded keydown is taken by the selector again (a loop) then broken
 * - if ArrowDown on a deck 2 jog leaf does not send one deck 2 tempo nudge with direction -1 then broken
 * - if an arrow on a leaf with no knob, slider or jog stops failing loud in the leaf host then broken
 * - if a leaf's data-sel-knob element stops mapping the arrows to that registered knob then broken
 * - if a mixer channel dial leaf stops stepping its knob by KNOB_CFG.keyStep then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const routing = await loadTypeScriptModule('src/lib/rb/performance-shortcut-routing.ts');
const sessions = await loadTypeScriptModule('src/lib/keyboard/selector-session.ts');
const registry = await loadTypeScriptModule('src/lib/keyboard/command-registry.ts');
const activate = await loadTypeScriptModule('src/lib/keyboard/selector-activate.ts');
const register = await loadTypeScriptModule('src/lib/keyboard/selector-register.ts');

// knob-control binds a window keydown listener and wheel-adjust reads localStorage at import.
const store = new Map();
globalThis.window = {
	addEventListener: () => {},
	removeEventListener: () => {},
	localStorage: {
		getItem: (key) => (store.has(key) ? store.get(key) : null),
		setItem: (key, value) => store.set(key, String(value)),
		removeItem: (key) => store.delete(key)
	}
};
const knobs = await loadTypeScriptModule('src/lib/rb/knob-control.svelte.ts');

const node = (id, parentId, kind, x, y) => ({
	id,
	parentId,
	kind,
	enabled: true,
	rect: { x, y, width: 100, height: 100 }
});

const LAYOUT = [
	node('decks', null, 'section', 0, 0),
	node('deck.1', 'decks', 'group', 0, 0),
	node('deck.2', 'decks', 'group', 600, 0),
	node('mixer', 'decks', 'group', 300, 0),
	node('deck.1.pitch', 'deck.1', 'leaf', 0, 0),
	node('deck.1.transport', 'deck.1', 'leaf', 0, 200),
	node('deck.2.jog', 'deck.2', 'leaf', 600, 0),
	node('deck.2.jog.dial', 'deck.2', 'leaf', 600, 200),
	node('mixer.crossfader', 'mixer', 'leaf', 300, 0),
	node('mixer.headphones', 'mixer', 'leaf', 300, 200),
	node('mixer.ch1.high', 'mixer', 'leaf', 300, 400)
];

const PAGE = { tagName: 'DIV', getAttribute: () => null };

/** A KeyboardEvent stand-in for node, which has Event but no KeyboardEvent. */
class FakeKeyboardEvent extends Event {
	constructor(type, init) {
		super(type, init);
		this.key = init.key;
	}
}

/** A fake control with attributes; a slider records the keydowns dispatched to it. */
function control(tagName, attrs = {}) {
	const keydowns = [];
	return {
		keydowns,
		tagName,
		getAttribute: (name) => attrs[name] ?? null,
		click: () => {},
		focus: () => {},
		dispatchEvent: (event) => {
			keydowns.push(event);
			return true;
		}
	};
}

/** A fake leaf element: `first` is its first control, `knobChild` its `data-sel-knob` element. */
function leaf(id, first, knobChild = null) {
	return {
		getAttribute: (name) => (name === 'data-sel-node' ? id : null),
		matches: () => false,
		querySelector: (selector) => {
			if (selector === '[data-sel-primary]') return null;
			if (selector === '[data-sel-knob]') return knobChild;
			return first;
		}
	};
}

/** Wires router, session, registry and leaf host the way installPerformanceHotkeys does. */
function harness() {
	knobs._resetKnobRegistryForTests();
	const dials = { '1:high': 0.5, '0:cue-mix': 0.5 };
	for (const id of Object.keys(dials))
		knobs.registerKnob({ id, getValue: () => dials[id], setValue: (v) => (dials[id] = v) });
	const pitch = control('DIV', { role: 'slider', 'aria-orientation': 'vertical' });
	const xfader = control('DIV', { role: 'slider', 'aria-orientation': 'horizontal' });
	const knobChild = control('DIV', { 'data-sel-knob': '0:cue-mix' });
	const leaves = new Map([
		['deck.1.pitch', leaf('deck.1.pitch', pitch)],
		['deck.1.transport', leaf('deck.1.transport', control('BUTTON'))],
		['mixer.crossfader', leaf('mixer.crossfader', xfader)],
		['mixer.headphones', leaf('mixer.headphones', null, knobChild)],
		['mixer.ch1.high', leaf('mixer.ch1.high', control('DIV', { role: 'slider' }))]
	]);
	const nudges = [];
	const deps = {
		findLeaf: (id) => leaves.get(id) ?? null,
		readControl: knobs.readKnobValue,
		nudgeControl: knobs.nudgeKnobOrSelection,
		nudgeTempo: (deck, direction) => nudges.push({ deck, direction }),
		keyStep: knobs.KNOB_CFG.keyStep
	};
	const session = sessions.createSelectorSession({
		snapshot: () => register.resolveSelectorNodes(LAYOUT),
		version: () => 1,
		showRing: () => {},
		hideRing: () => {}
	});
	const handlers = {
		selectorCommand: (command) => session.run(command, { timeStamp: 0, startId: null }),
		clearSelector: () => session.clear(),
		selectorLeafCommand: (command) =>
			registry.runCommand(command, {
				timeStamp: 0,
				selectorLeaf: activate.createSelectorLeafHost(session.selectedLeaf(), deps)
			})
	};
	const route = (event) => {
		const leafId = session.selectedLeaf();
		return routing.handlePerformanceShortcutKeydown(event, handlers, {
			selectorActive: session.selected() !== null,
			selectorInLibrary: session.selectionWithin('library'),
			selectorLeafId: leafId,
			selectorLeafAdjustable: (id) => activate.selectorLeafAdjustTarget(id, deps) !== null
		});
	};
	const press = (key) =>
		route({
			code: '',
			key,
			target: PAGE,
			metaKey: false,
			ctrlKey: false,
			altKey: false,
			shiftKey: false,
			repeat: false,
			timeStamp: 0,
			preventDefault: () => {},
			stopPropagation: () => {}
		});
	const select = (id) => {
		session.clear();
		session.run('selector.drill-in', { timeStamp: 0, startId: id });
		assert.equal(session.selectedLeaf(), id);
	};
	return { dials, pitch, xfader, nudges, deps, route, press, select };
}

/** Swaps in the KeyboardEvent stand-in for one test; returns the restore. */
function withKeyboardEvent() {
	const saved = globalThis.KeyboardEvent;
	globalThis.KeyboardEvent = FakeKeyboardEvent;
	return () => {
		if (saved === undefined) delete globalThis.KeyboardEvent;
		else globalThis.KeyboardEvent = saved;
	};
}

// [if] the crossfader or a pitch fader leaf is selected and ArrowUp is pressed [then] its slider gets one up keydown, no error, [else stop].
test('KEYB-14324 ArrowUp on a selected pitch or crossfader leaf forwards one keydown to its slider', (t) => {
	t.after(withKeyboardEvent());
	const h = harness();
	h.select('deck.1.pitch');
	assert.equal(h.press('ArrowUp'), true);
	assert.deepEqual(
		h.pitch.keydowns.map((e) => [e.type, e.key, e.bubbles]),
		[['keydown', 'ArrowUp', true]]
	);
	// The forwarded keydown reaches the window listener too; the selector must let it through.
	assert.equal(h.route(h.pitch.keydowns[0]), false);
	assert.equal(h.pitch.keydowns.length, 1);
	// The crossfader is horizontal: its own handler steps on Right and Left.
	h.select('mixer.crossfader');
	assert.equal(h.press('ArrowUp'), true);
	assert.equal(h.press('ArrowDown'), true);
	assert.deepEqual(
		h.xfader.keydowns.map((e) => e.key),
		['ArrowRight', 'ArrowLeft']
	);
});

// [if] a jog leaf for deck 2 is selected and ArrowDown is pressed [then] one deck 2 tempo nudge with direction -1 is sent, [else stop].
test('KEYB-14324 ArrowDown on the deck 2 jog leaf sends one deck 2 tempo nudge down', () => {
	const h = harness();
	h.select('deck.2.jog');
	assert.equal(h.press('ArrowDown'), true);
	assert.deepEqual(h.nudges, [{ deck: 2, direction: -1 }]);
	h.select('deck.2.jog.dial');
	assert.equal(h.press('ArrowUp'), true);
	assert.deepEqual(h.nudges, [
		{ deck: 2, direction: -1 },
		{ deck: 2, direction: 1 }
	]);
	assert.equal(activate.selectorLeafJogDeck('deck.5.jog'), null);
	assert.equal(activate.selectorLeafJogDeck('deck.1.jog.ring'), null);
});

// [if] a leaf with no knob, slider or jog is selected and an arrow is pressed [then] the clear "no registered dial or fader" error stays, [else stop].
test('KEYB-14324 a leaf with no knob, slider or jog keeps the arrows and its clear error', () => {
	const h = harness();
	h.select('deck.1.transport');
	assert.equal(h.press('ArrowUp'), false, 'the page keeps the arrows');
	const host = activate.createSelectorLeafHost('deck.1.transport', h.deps);
	assert.throws(
		() => host.adjust(1),
		/Selector leaf deck\.1\.transport has no registered dial or fader to adjust/
	);
	assert.throws(
		() => activate.createSelectorLeafHost('mixer.missing', h.deps).adjust(-1),
		/no registered dial or fader to adjust/
	);
});

// [if] a leaf holds an element with data-sel-knob naming a registered knob and ArrowUp is pressed [then] that knob steps up one key step, [else stop].
test('KEYB-14324 a data-sel-knob element maps its leaf to that registered knob', () => {
	const h = harness();
	h.select('mixer.headphones');
	assert.equal(h.press('ArrowUp'), true);
	const step = knobs.KNOB_CFG.keyStep;
	assert.ok(Math.abs(h.dials['0:cue-mix'] - (0.5 + step)) < 1e-9, String(h.dials['0:cue-mix']));
	knobs.unregisterKnob('0:cue-mix');
	assert.equal(h.press('ArrowUp'), false, 'an unregistered knob id is not adjustable');
});

// [if] a mixer channel dial leaf is selected [then] the arrows still nudge its registered knob by one key step, [else stop].
test('KEYB-14324 a mixer channel dial leaf still steps its registered knob', () => {
	const h = harness();
	h.select('mixer.ch1.high');
	const step = knobs.KNOB_CFG.keyStep;
	assert.equal(h.press('ArrowUp'), true);
	assert.ok(Math.abs(h.dials['1:high'] - (0.5 + step)) < 1e-9, String(h.dials['1:high']));
	assert.equal(h.press('ArrowLeft'), true);
	assert.ok(Math.abs(h.dials['1:high'] - 0.5) < 1e-9, String(h.dials['1:high']));
});
