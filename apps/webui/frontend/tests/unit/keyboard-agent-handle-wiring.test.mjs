// requirement: KEYB-13838, KEYB-13983, KEYB-14000, KEYB-14023, KEYB-14370
/**
 * #13770: installPerformanceHotkeys registers its live selector session and shortcut handlers
 * behind `window.__odjKeyboard`, so an agent's run(id) drives what the keys drive.
 * The real installer, session, keymap runtime and dispatcher run; only the DOM is synthesized.
 *
 * Regression lines:
 * - if run('selector.move.right') does not move the W A S D session and its ring then broken
 * - if run('transport.toggle') does not dispatch what Space dispatches then broken
 * - if teardown leaves either provider registered then broken
 * - if selector() does not read the live session, or still reports a removed node, then broken
 * - if two-deck mode hides the selected deck 3 leaf and the ring or a leaf command stays on it then broken
 * - if run('selector.activate') does not click the control Enter clicks then broken
 * - if run('selector.adjust.up') does not step the dial by what the Up arrow steps then broken
 * - if a leaf command with no leaf selected throws instead of refusing then broken
 * - if ArrowDown on a jog leaf does not run the keymap's own tempo nudge down for that deck then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

class FakeElement {}
const listeners = new Map();
let m;

/** A registered selector node: a section box at x, measured like a laid-out element. */
function sectionElement(x) {
	const el = new FakeElement();
	el.parentElement = null;
	el.setAttribute = () => {};
	el.removeAttribute = () => {};
	el.toggleAttribute = () => {};
	el.getBoundingClientRect = () => ({ x, y: 0, width: 100, height: 100 });
	return el;
}

/**
 * A leaf registered under `parent`, findable by `data-sel-node`, with one button that counts clicks.
 * The node is unregistered and the page lookup removed by `dispose`.
 */
function leafUnder(parent, id) {
	const el = sectionElement(0);
	el.parentElement = { closest: () => parent };
	const control = { tagName: 'BUTTON', getAttribute: () => null, clicks: 0 };
	control.click = () => {
		control.clicks += 1;
	};
	el.getAttribute = () => null;
	el.matches = () => false;
	el.querySelector = () => control;
	const node = m.selectorNode(el, { id, kind: 'leaf', enabled: true });
	globalThis.CSS = { escape: (value) => value };
	globalThis.document = {
		querySelector: (sel) => (sel === `[data-sel-node="${id}"]` ? el : null)
	};
	return {
		control,
		dispose: () => {
			node.destroy();
			delete globalThis.document;
			delete globalThis.CSS;
		}
	};
}

/** Select the first leaf of section 'left' through the agent handle, as A and E keys would. */
function selectLeftLeaf(handle) {
	handle.run('selector.move.right');
	handle.run('selector.drill-in');
}

function keydown(key, code) {
	const event = {
		key,
		code,
		target: { tagName: 'DIV', getAttribute: () => null },
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		shiftKey: false,
		repeat: false,
		timeStamp: performance.now(),
		preventDefault: () => {}
	};
	for (const listener of listeners.get('keydown') ?? []) listener(event);
}

/** The performance-ipc debug rows a call leaves once its async dispatch has settled. */
async function dispatchRows(call) {
	const rows = [];
	const original = console.debug;
	console.debug = (...args) => {
		const line = args.map(String).join(' ');
		if (line.startsWith('[performance-ipc]')) rows.push(line);
	};
	try {
		call();
		for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
	} finally {
		console.debug = original;
	}
	return rows;
}

function agentHandle() {
	return m.createKeyboardAgentHandle({
		commands: m.KEYMAP_COMMANDS,
		activeKeymap: m.activeKeyboardKeymap,
		runKeymapTarget: m.runKeymapTarget
	});
}

let leftSection;

before(async () => {
	const storage = new Map();
	globalThis.window = globalThis;
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = class extends FakeElement {};
	globalThis.getComputedStyle = () => ({ display: 'block' });
	globalThis.localStorage = {
		getItem: (k) => storage.get(k) ?? null,
		setItem: (k, v) => storage.set(k, String(v)),
		removeItem: (k) => storage.delete(k)
	};
	globalThis.addEventListener = (type, fn) => {
		listeners.set(type, [...(listeners.get(type) ?? []), fn]);
	};
	globalThis.removeEventListener = (type, fn) => {
		listeners.set(
			type,
			(listeners.get(type) ?? []).filter((f) => f !== fn)
		);
	};
	m = await loadTypeScriptModule('tests/unit/fixtures/keyboard-agent-handle-wiring-entry.ts');
	leftSection = sectionElement(0);
	m.selectorNode(leftSection, { id: 'left', kind: 'section', enabled: true });
	m.selectorNode(sectionElement(200), { id: 'right', kind: 'section', enabled: true });
});

test("run('selector.move.right') moves the W A S D session's selection and the ring follows", () => {
	/** [if] hotkeys are installed and run('selector.move.right') is called [then] the key session's selection and ring move, [else stop]. */
	const uninstall = m.installPerformanceHotkeys();
	try {
		const handle = agentHandle();
		// The first selector command reveals the start node, exactly as the first key does.
		assert.deepEqual(handle.run('selector.move.right'), {
			ok: true,
			id: 'selector.move.right',
			selected: 'left'
		});
		assert.deepEqual(handle.run('selector.move.right'), {
			ok: true,
			id: 'selector.move.right',
			selected: 'right'
		});
		assert.equal(m.selectorRing.target.id, 'right');
		keydown('a', 'KeyA');
		assert.equal(m.selectorRing.target.id, 'left', 'the A key moves the selector the agent moved');
	} finally {
		uninstall();
	}
});

test("run('transport.toggle') runs the same handler Space runs", async () => {
	/** [if] run('transport.toggle') is called with hotkeys installed [then] it dispatches what Space dispatches, [else stop]. */
	const uninstall = m.installPerformanceHotkeys();
	const pending = m.beginPendingLoadPlay(1, false);
	try {
		const space = await dispatchRows(() => keydown(' ', 'Space'));
		assert.equal(space.length, 1, `Space dispatches one command: ${space}`);
		assert.match(space[0], /load_play_intent/);
		let result;
		const agent = await dispatchRows(() => {
			result = agentHandle().run('transport.toggle');
		});
		assert.deepEqual(result, { ok: true, id: 'transport.toggle', selected: null });
		assert.deepEqual(agent, space);
	} finally {
		m.clearPendingLoadPlay(1, pending.generation);
		uninstall();
	}
});

test('teardown resets both providers so run refuses again', () => {
	/** [if] the hotkeys are torn down [then] selector and legacy keymap commands are refused by name again, [else stop]. */
	const uninstall = m.installPerformanceHotkeys();
	const handle = agentHandle();
	assert.equal(handle.run('selector.move.right').ok, true);
	uninstall();
	assert.deepEqual(handle.run('selector.move.right'), {
		ok: false,
		id: 'selector.move.right',
		refusal: 'no_selector'
	});
	assert.deepEqual(handle.run('transport.toggle'), {
		ok: false,
		id: 'transport.toggle',
		refusal: 'no_shortcut_handlers'
	});
});

test('selector() reads the live session as plain JSON and moves off a removed node', () => {
	/** [if] the session selects a root that is then unregistered [then] selector() reports the first root instead, [else stop]. */
	const uninstall = m.installPerformanceHotkeys();
	const far = m.selectorNode(sectionElement(400), { id: 'far', kind: 'section', enabled: true });
	try {
		const handle = agentHandle();
		assert.equal(handle.selector().selectedId, null, 'nothing is selected before the first key');
		handle.run('selector.move.right');
		handle.run('selector.move.right');
		const live = handle.selector();
		assert.deepEqual(JSON.parse(JSON.stringify(live)), live);
		assert.deepEqual(live, {
			selectedId: 'right',
			present: true,
			kind: 'section',
			parentId: null,
			operations: ['selector.move.left', 'selector.move.right']
		});
		handle.run('selector.move.right');
		far.destroy();
		// #12256: a removed root has no surviving ancestor, so the selection goes to the first root.
		assert.deepEqual(handle.selector(), {
			selectedId: 'left',
			present: true,
			kind: 'section',
			parentId: null,
			operations: ['selector.move.right']
		});
	} finally {
		uninstall();
	}
});

test('two-deck mode hiding the selected deck 3 leaf moves the selection, ring and commands off it', async () => {
	/** [if] two-deck mode disables deck 3 while its leaf is selected [then] ring and selection go to the section, leaf runs refuse, [else stop]. */
	const deck3El = sectionElement(0);
	deck3El.parentElement = { closest: () => leftSection };
	const deck3 = m.selectorNode(deck3El, { id: 'deck.3', kind: 'group', enabled: true });
	const leaf = leafUnder(deck3El, 'deck.3.transport');
	const uninstall = m.installPerformanceHotkeys();
	try {
		const handle = agentHandle();
		selectLeftLeaf(handle);
		handle.run('selector.drill-in');
		assert.equal(m.selectorRing.target?.id, 'deck.3.transport');
		deck3.update({ id: 'deck.3', kind: 'group', enabled: false });
		await Promise.resolve();
		assert.equal(m.selectorRing.target?.id, 'left', 'the ring leaves the hidden deck unprompted');
		assert.equal(handle.selector().selectedId, 'left');
		assert.deepEqual(handle.run('selector.activate'), {
			ok: false,
			id: 'selector.activate',
			refusal: 'no_leaf_selected'
		});
		assert.equal(leaf.control.clicks, 0, 'the hidden control was never pressed');
	} finally {
		uninstall();
		leaf.dispose();
		deck3.destroy();
	}
});

test("run('selector.activate') clicks the same control Enter clicks", () => {
	/** [if] a leaf is selected and run('selector.activate') is called [then] the control Enter clicks is clicked, [else stop]. */
	const leaf = leafUnder(leftSection, 'left.play');
	const uninstall = m.installPerformanceHotkeys();
	try {
		const handle = agentHandle();
		selectLeftLeaf(handle);
		assert.equal(handle.selector().selectedId, 'left.play');
		keydown('Enter', 'Enter');
		assert.equal(leaf.control.clicks, 1, 'Enter clicks the leaf control');
		assert.deepEqual(handle.run('selector.activate'), {
			ok: true,
			id: 'selector.activate',
			selected: 'left.play'
		});
		assert.equal(leaf.control.clicks, 2, 'run clicks the same control once more');
	} finally {
		uninstall();
		leaf.dispose();
	}
});

test("run('selector.adjust.up') moves the fader leaf by the Up arrow's step", () => {
	/** [if] a fader leaf is selected and run('selector.adjust.up') is called [then] it moves by the Up arrow's step, [else stop]. */
	const leaf = leafUnder(leftSection, 'mixer.ch1.fader');
	let value = 0.5;
	m.registerKnob({ id: '1:fader', getValue: () => value, setValue: (v) => (value = v) });
	const uninstall = m.installPerformanceHotkeys();
	try {
		const handle = agentHandle();
		selectLeftLeaf(handle);
		keydown('ArrowUp', 'ArrowUp');
		const arrowStep = value - 0.5;
		assert.ok(Math.abs(arrowStep - m.KNOB_CFG.keyStep) < 1e-9, `Up arrow steps: ${arrowStep}`);
		const before = value;
		assert.deepEqual(handle.run('selector.adjust.up'), {
			ok: true,
			id: 'selector.adjust.up',
			selected: 'mixer.ch1.fader'
		});
		assert.ok(Math.abs(value - before - arrowStep) < 1e-9, `run steps: ${value - before}`);
	} finally {
		uninstall();
		m.unregisterKnob('1:fader');
		leaf.dispose();
	}
});

test('ArrowDown on the deck 2 jog leaf runs the same tempo nudge as the keymap', () => {
	/** [if] the deck 2 jog leaf is selected and ArrowDown is pressed [then] it runs the keymap's deck 2 tempo_down, [else stop]. */
	const leaf = leafUnder(leftSection, 'deck.2.jog');
	const deck = m.deckStates[2];
	const saved = { stable_id: deck.stable_id, bpm: deck.bpm, pitch: deck.pitch };
	// At the bottom of the pitch range a nudge down is refused with a toast; a nudge up is not.
	Object.assign(deck, { stable_id: 'jog-test', bpm: 120, pitch: 1 - m.pitchRanges[2] / 100 });
	const uninstall = m.installPerformanceHotkeys();
	const toastsFrom = (call) => {
		const before = m.toasts.length;
		call();
		return m.toasts.slice(before).map((toast) => toast.message);
	};
	try {
		selectLeftLeaf(agentHandle());
		const arrow = toastsFrom(() => keydown('ArrowDown', 'ArrowDown'));
		assert.deepEqual(arrow, ['Deck 2: tempo nudge exceeds the selected pitch range']);
		const keymap = toastsFrom(() => m.runKeymapTarget('deck.2.tempo_down', 0, null));
		assert.deepEqual(arrow, keymap);
	} finally {
		uninstall();
		Object.assign(deck, saved);
		leaf.dispose();
	}
});

test('leaf commands with no leaf selected return a refusal instead of throwing', () => {
	/** [if] no leaf is selected and a leaf command is run by id [then] run returns no_leaf_selected and does not throw, [else stop]. */
	const uninstall = m.installPerformanceHotkeys();
	try {
		const handle = agentHandle();
		for (const id of ['selector.activate', 'selector.adjust.up', 'selector.adjust.down']) {
			assert.deepEqual(handle.run(id), { ok: false, id, refusal: 'no_leaf_selected' });
		}
		handle.run('selector.move.right');
		assert.equal(handle.selector().kind, 'section', 'a section, not a leaf, is selected');
		assert.deepEqual(handle.run('selector.activate'), {
			ok: false,
			id: 'selector.activate',
			refusal: 'no_leaf_selected'
		});
	} finally {
		uninstall();
	}
});
