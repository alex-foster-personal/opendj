/**
 * #13708: Enter activates and the arrows adjust the selected selector leaf (selector-activate.ts).
 *
 * The real router, selector session, command registry, leaf host and knob registry run together;
 * only the DOM (the node snapshot, the leaf element and its control) is a fixture.
 *
 * Regression lines:
 * - if Enter on a selected button leaf does not click its primary control exactly once then broken
 * - if ArrowUp on a selected dial leaf does not step that knob by KNOB_CFG.keyStep then broken
 * - if Enter with no leaf selected (none, a group, the library) is taken from its existing owner then broken
 * - if a focused text input loses Enter or the arrows to the selector then broken
 * - if a leaf id stops mapping to its knob registry id then the arrows step nothing
 * - if the agent registry stops listing selector.activate / selector.adjust.* with their keys then broken
 * - if a focused button, range input or select outside the leaf loses Enter or the arrows to the selector then broken
 * - if Enter from the page body stops activating the selected leaf then broken
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

const node = (id, parentId, kind, x, y, width, height) => ({
	id,
	parentId,
	kind,
	enabled: true,
	rect: { x, y, width, height }
});

const LAYOUT = [
	node('decks', null, 'section', 0, 240, 1600, 400),
	node('library', null, 'section', 0, 640, 1600, 400),
	node('deck.1', 'decks', 'group', 0, 240, 600, 200),
	node('mixer', 'decks', 'group', 600, 240, 400, 400),
	node('deck.1.transport', 'deck.1', 'leaf', 0, 340, 200, 100),
	node('mixer.ch1', 'mixer', 'group', 600, 240, 100, 400),
	node('mixer.ch1.high', 'mixer.ch1', 'leaf', 600, 300, 100, 60)
];

const PAGE = { tagName: 'DIV', getAttribute: () => null };
const TEXT = { tagName: 'INPUT', type: 'text', getAttribute: () => null };

/** A fake control that counts clicks and focuses. */
function control(tagName, role = null) {
	const counts = { clicks: 0, focuses: 0 };
	return {
		counts,
		tagName,
		getAttribute: (name) => (name === 'role' ? role : null),
		click: () => (counts.clicks += 1),
		focus: () => (counts.focuses += 1)
	};
}

/** A fake leaf element whose first control is `first`. */
function leaf(id, first) {
	return {
		getAttribute: (name) => (name === 'data-sel-node' ? id : null),
		matches: () => false,
		querySelector: (selector) => (selector.startsWith('[data-sel-primary]') ? null : first)
	};
}

/** Wires router, session, registry and leaf host the way installPerformanceHotkeys does. */
function harness() {
	knobs._resetKnobRegistryForTests();
	const dial = { value: 0.5 };
	knobs.registerKnob({
		id: '1:high',
		getValue: () => dial.value,
		setValue: (v) => (dial.value = v)
	});
	const play = control('BUTTON');
	const leaves = new Map([
		['deck.1.transport', leaf('deck.1.transport', play)],
		['mixer.ch1.high', leaf('mixer.ch1.high', control('DIV', 'slider'))]
	]);
	const deps = {
		findLeaf: (id) => leaves.get(id) ?? null,
		readControl: knobs.readKnobValue,
		nudgeControl: knobs.nudgeKnobOrSelection,
		nudgeTempo: () => {
			throw new Error('no jog leaf in this layout');
		},
		keyStep: knobs.KNOB_CFG.keyStep
	};
	const session = sessions.createSelectorSession({
		snapshot: () => register.resolveSelectorNodes(LAYOUT),
		version: () => 1,
		showRing: () => {},
		hideRing: () => {}
	});
	const state = { dial, play, plays: 0 };
	const press = (key, { target = PAGE, startId = null, repeat = false } = {}) => {
		let prevented = 0;
		let stopped = 0;
		const event = {
			code: key === ' ' ? 'Space' : '',
			key,
			target,
			metaKey: false,
			ctrlKey: false,
			altKey: false,
			shiftKey: false,
			repeat,
			timeStamp: 0,
			preventDefault: () => (prevented += 1),
			stopPropagation: () => (stopped += 1)
		};
		const handlers = {
			toggleRecentPlay: () => (state.plays += 1),
			selectorCommand: (command) => session.run(command, { timeStamp: 0, startId }),
			clearSelector: () => session.clear(),
			selectorLeafCommand: (command) =>
				registry.runCommand(command, {
					timeStamp: 0,
					selectorLeaf: activate.createSelectorLeafHost(session.selectedLeaf(), deps)
				})
		};
		const handled = routing.handlePerformanceShortcutKeydown(event, handlers, {
			selectorActive: session.selected() !== null,
			selectorInLibrary: session.selectionWithin('library'),
			selectorLeafId: session.selectedLeaf(),
			selectorLeafAdjustable: (id) => activate.selectorLeafAdjustTarget(id, deps) !== null
		});
		return { handled, prevented, stopped };
	};
	return { state, session, press };
}

// [if] a button leaf is selected and Enter is pressed [then] its primary control is clicked once, [else stop].
test('KEYB-13777 Enter on a selected button leaf clicks its primary control once', () => {
	const { state, session, press } = harness();
	press('e', { startId: 'deck.1.transport' });
	assert.equal(session.selectedLeaf(), 'deck.1.transport');
	assert.deepEqual(press('Enter'), { handled: true, prevented: 1, stopped: 1 });
	assert.deepEqual(state.play.counts, { clicks: 1, focuses: 0 });
	press('Enter', { repeat: true });
	assert.equal(state.play.counts.clicks, 1);
});

// [if] a dial leaf (mixer.ch1.high) is selected and ArrowUp is pressed [then] that knob steps up by one key step, [else stop].
test('KEYB-13777 ArrowUp on the mixer.ch1.high leaf steps that knob up by one key step', () => {
	const { state, press } = harness();
	const step = knobs.KNOB_CFG.keyStep;
	press('e', { startId: 'mixer.ch1.high' });
	assert.deepEqual(press('ArrowUp'), { handled: true, prevented: 1, stopped: 1 });
	assert.ok(Math.abs(state.dial.value - (0.5 + step)) < 1e-9, String(state.dial.value));
	press('ArrowRight', { repeat: true });
	press('ArrowDown');
	press('ArrowLeft');
	assert.ok(Math.abs(state.dial.value - 0.5) < 1e-9, String(state.dial.value));
});

// [if] no selector leaf is selected and Enter is pressed [then] the existing Enter behaviour runs unchanged, [else stop].
test('KEYB-13777 Enter with no leaf, a group or the library selected is left to its owner', () => {
	const { state, session, press } = harness();
	assert.deepEqual(press('Enter'), { handled: false, prevented: 0, stopped: 0 });
	press('e', { startId: 'deck.1' });
	assert.equal(session.selected(), 'deck.1');
	assert.deepEqual(press('Enter'), { handled: false, prevented: 0, stopped: 0 });
	assert.deepEqual(press('ArrowUp'), { handled: false, prevented: 0, stopped: 0 });
	session.clear();
	press('e', { startId: 'library' });
	assert.deepEqual(press('Enter'), { handled: false, prevented: 0, stopped: 0 });
	// A leaf with no dial behind it keeps the arrows for the page.
	session.clear();
	press('e', { startId: 'deck.1.transport' });
	assert.deepEqual(press('ArrowUp'), { handled: false, prevented: 0, stopped: 0 });
	assert.equal(state.play.counts.clicks, 0);
});

// [if] a text input has focus [then] Enter and arrows are not taken by the selector, [else stop].
test('KEYB-13777 a focused text input keeps Enter and the arrows', () => {
	const { state, press } = harness();
	press('e', { startId: 'mixer.ch1.high' });
	for (const key of ['Enter', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'])
		assert.deepEqual(
			press(key, { target: TEXT }),
			{ handled: false, prevented: 0, stopped: 0 },
			key
		);
	assert.equal(state.dial.value, 0.5);
});

// [if] focus sits inside an open menu while a leaf is selected [then] the menu keeps Enter and the arrows, [else stop].
test('KEYB-13777 an open menu keeps Enter and the arrows from the selected leaf', () => {
	const { state, press } = harness();
	press('e', { startId: 'mixer.ch1.high' });
	const menuItem = {
		tagName: 'BUTTON',
		getAttribute: () => 'menuitem',
		closest: (selector) => (selector.includes('[role="menu"]') ? menuItem : null)
	};
	for (const key of ['Enter', 'ArrowDown'])
		assert.deepEqual(
			press(key, { target: menuItem }),
			{ handled: false, prevented: 0, stopped: 0 },
			key
		);
	assert.equal(state.dial.value, 0.5);
});

// [if] a mixer leaf id is mapped [then] it gives the id knob-control registers for that dial or fader, [else stop].
test('KEYB-13777 leaf ids map to knob registry ids; Enter focuses a slider and fails loud with no control', () => {
	assert.equal(activate.selectorLeafControlId('mixer.ch1.high'), knobs.knobId(1, 'high'));
	assert.equal(activate.selectorLeafControlId('mixer.ch3.filter'), knobs.knobId(3, 'filter'));
	assert.equal(activate.selectorLeafControlId('mixer.ch2.fader'), knobs.faderControlId(2));
	assert.equal(
		activate.selectorLeafControlId('mixer.ch4.stem.vocal'),
		knobs.knobId(4, knobs.stemKnobRole('vocal'))
	);
	assert.equal(activate.selectorLeafControlId('deck.1.transport'), null);
	assert.equal(activate.selectorLeafControlId('mixer.crossfader'), null);
	const slider = control('DIV', 'slider');
	assert.equal(activate.activateSelectorLeaf(leaf('mixer.ch1.high', slider)), 'focus');
	assert.deepEqual(slider.counts, { clicks: 0, focuses: 1 });
	assert.throws(
		() => activate.activateSelectorLeaf(leaf('deck.1.jog', null)),
		/no control to activate/
	);
});

// [if] an agent lists the registry [then] selector.activate and selector.adjust.* appear with Enter and arrow keys, [else stop].
test('KEYB-13777 the registry lists the leaf commands with their keys and fails loud without a leaf', () => {
	const byId = new Map(registry.listPerformanceCommandMetadata().map((c) => [c.id, c.defaultKeys]));
	assert.deepEqual(byId.get('selector.activate'), ['Enter']);
	assert.deepEqual(byId.get('selector.adjust.up'), ['ArrowUp', 'ArrowRight']);
	assert.deepEqual(byId.get('selector.adjust.down'), ['ArrowDown', 'ArrowLeft']);
	for (const id of registry.SELECTOR_LEAF_COMMAND_IDS)
		assert.throws(() => registry.runCommand(id, { timeStamp: 0 }), /no selected leaf/);
});

/** Swaps in DOM class stand-ins so isNativeInteractiveTarget can run under node; returns the restore. */
function withDomClasses() {
	const saved = { Element: globalThis.Element, HTMLElement: globalThis.HTMLElement };
	class Element {}
	class HTMLElement extends Element {}
	globalThis.Element = Element;
	globalThis.HTMLElement = HTMLElement;
	const restore = () => Object.assign(globalThis, saved);
	return { HTMLElement, restore };
}

/** A fake DOM element: attributes, a parent chain for closest('[data-sel-node]'), click and focus counts. */
function domElement(HTMLElement, tagName, attrs = {}, parent = null) {
	const el = new HTMLElement();
	const counts = { clicks: 0, focuses: 0 };
	Object.assign(el, {
		counts,
		tagName,
		type: attrs.type ?? '',
		isContentEditable: false,
		parent,
		getAttribute: (name) => attrs[name] ?? null,
		hasAttribute: (name) => name in attrs,
		closest: (selector) => {
			if (selector !== '[data-sel-node]') return null;
			for (let at = el; at !== null; at = at.parent)
				if (at.hasAttribute('data-sel-node')) return at;
			return null;
		},
		querySelector: () => null,
		click: () => (counts.clicks += 1),
		focus: () => (counts.focuses += 1)
	});
	return el;
}

// [if] a leaf is selected and a different button has focus [then] Enter is left to the button and the leaf is not activated, [else stop].
test('KEYB-14141 a focused button outside the selected leaf keeps Enter', (t) => {
	const { HTMLElement, restore } = withDomClasses();
	t.after(restore);
	const { state, session, press } = harness();
	press('e', { startId: 'deck.1.transport' });
	assert.equal(session.selectedLeaf(), 'deck.1.transport');
	const other = domElement(HTMLElement, 'DIV', { 'data-sel-node': 'deck.1.sync' });
	const button = domElement(HTMLElement, 'BUTTON', {}, other);
	assert.deepEqual(press('Enter', { target: button }), {
		handled: false,
		prevented: 0,
		stopped: 0
	});
	assert.equal(state.play.counts.clicks, 0);
	// The leaf's own control agrees with the selector, so it keeps today's behaviour.
	const own = domElement(HTMLElement, 'DIV', { 'data-sel-node': 'deck.1.transport' });
	const ownButton = domElement(HTMLElement, 'BUTTON', {}, own);
	assert.deepEqual(press('Enter', { target: ownButton }), {
		handled: true,
		prevented: 1,
		stopped: 1
	});
	assert.equal(state.play.counts.clicks, 1);
});

// [if] a dial leaf is selected and a range input or select has focus [then] the arrows are left to that control, [else stop].
test('KEYB-14141 a focused range input or select keeps the arrows from the selected dial leaf', (t) => {
	const { HTMLElement, restore } = withDomClasses();
	t.after(restore);
	const { state, press } = harness();
	press('e', { startId: 'mixer.ch1.high' });
	const range = domElement(HTMLElement, 'INPUT', { type: 'range' });
	const select = domElement(HTMLElement, 'SELECT');
	for (const target of [range, select])
		for (const key of ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Enter'])
			assert.deepEqual(
				press(key, { target }),
				{ handled: false, prevented: 0, stopped: 0 },
				`${target.tagName} ${key}`
			);
	assert.equal(state.dial.value, 0.5);
});

// [if] a leaf is selected and focus is on the page body [then] Enter still activates the leaf, as today, [else stop].
test('KEYB-14141 Enter from the page body still activates the selected leaf', (t) => {
	const { HTMLElement, restore } = withDomClasses();
	t.after(restore);
	const { state, press } = harness();
	press('e', { startId: 'deck.1.transport' });
	const body = domElement(HTMLElement, 'BODY');
	assert.deepEqual(press('Enter', { target: body }), { handled: true, prevented: 1, stopped: 1 });
	assert.equal(state.play.counts.clicks, 1);
});
