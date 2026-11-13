// requirement: KEYB-14396
/**
 * #14379: a selector leaf registered directly on a control is that control (selector-activate.ts).
 *
 * PR #13865 puts `use:selectorNode` on the pad, grid edit, beat jump, UNDO and pad-mode buttons,
 * so the leaf element IS the button. The real router, selector session, command registry and
 * leaf host run together; only the DOM (the node snapshot and the elements) is a fixture, with a
 * small `matches` / `querySelector` that evaluates the real SELECTOR_CONTROL list.
 *
 * Regression lines:
 * - if Enter on a leaf that is itself a button does not click that button once then broken
 * - if a leaf that is itself a slider is not focused on Enter or does not get the forwarded arrow then broken
 * - if a disabled button leaf with no control inside stops failing with "has no control to activate" then broken
 * - if a leaf that wraps a button stops clicking the inner button then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const routing = await loadTypeScriptModule('src/lib/rb/performance-shortcut-routing.ts');
const sessions = await loadTypeScriptModule('src/lib/keyboard/selector-session.ts');
const registry = await loadTypeScriptModule('src/lib/keyboard/command-registry.ts');
const activate = await loadTypeScriptModule('src/lib/keyboard/selector-activate.ts');
const register = await loadTypeScriptModule('src/lib/keyboard/selector-register.ts');

const node = (id, x) => ({
	id,
	parentId: id === 'decks' ? null : 'decks',
	kind: id === 'decks' ? 'section' : 'leaf',
	enabled: true,
	rect: { x, y: 0, width: 100, height: 100 }
});

const LAYOUT = [
	node('decks', 0),
	node('deck.1.pad.1', 0),
	node('deck.1.pitch', 100),
	node('deck.1.undo', 200),
	node('deck.1.transport', 300)
];

const PAGE = { tagName: 'DIV', getAttribute: () => null };

/** One simple selector part: `tag`, `[attr]`, `[attr="v"]`, each with an optional `:not([...])`. */
const PART = /^(\w+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?(?::not\(\[([\w-]+)(?:="([^"]*)")?\]\))?$/;

/** True when `el` matches one simple part; throws on a selector the fixture cannot read. */
function _matchesPart(el, part) {
	const m = PART.exec(part.trim());
	if (m === null) throw new Error(`fixture cannot match selector part: ${part}`);
	const [, tag, attr, value, notAttr, notValue] = m;
	if (tag !== undefined && el.tagName !== tag.toUpperCase()) return false;
	if (attr !== undefined && !(attr in el.attrs)) return false;
	if (value !== undefined && el.attrs[attr] !== value) return false;
	if (notAttr !== undefined && notAttr in el.attrs) {
		if (notValue === undefined || el.attrs[notAttr] === notValue) return false;
	}
	return true;
}

/** A fake element with attributes and descendants; counts clicks, focuses and dispatched keydowns. */
function element(tagName, attrs = {}, descendants = []) {
	const el = {
		tagName,
		attrs,
		counts: { clicks: 0, focuses: 0 },
		keydowns: [],
		getAttribute: (name) => attrs[name] ?? null,
		matches: (selector) => selector.split(',').some((part) => _matchesPart(el, part)),
		querySelector: (selector) => descendants.find((d) => d.matches(selector)) ?? null,
		click: () => (el.counts.clicks += 1),
		focus: () => (el.counts.focuses += 1),
		dispatchEvent: (event) => {
			el.keydowns.push(event);
			return true;
		}
	};
	return el;
}

/** A KeyboardEvent stand-in for node, which has Event but no KeyboardEvent. */
class FakeKeyboardEvent extends Event {
	constructor(type, init) {
		super(type, init);
		this.key = init.key;
	}
}

/** Wires router, session, registry and leaf host the way installPerformanceHotkeys does. */
function harness() {
	const inner = element('BUTTON');
	const leaves = new Map([
		['deck.1.pad.1', element('BUTTON', { 'data-sel-node': 'deck.1.pad.1' })],
		[
			'deck.1.pitch',
			element('DIV', {
				'data-sel-node': 'deck.1.pitch',
				role: 'slider',
				'aria-orientation': 'vertical'
			})
		],
		['deck.1.undo', element('BUTTON', { 'data-sel-node': 'deck.1.undo', disabled: '' })],
		['deck.1.transport', element('DIV', { 'data-sel-node': 'deck.1.transport' }, [inner])]
	]);
	const deps = {
		findLeaf: (id) => leaves.get(id) ?? null,
		readControl: () => null,
		nudgeControl: () => {
			throw new Error('no knob in this layout');
		},
		nudgeTempo: () => {
			throw new Error('no jog leaf in this layout');
		},
		keyStep: 0.05
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
	const press = (key) =>
		routing.handlePerformanceShortcutKeydown(
			{
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
			},
			handlers,
			{
				selectorActive: session.selected() !== null,
				selectorInLibrary: session.selectionWithin('library'),
				selectorLeafId: session.selectedLeaf(),
				selectorLeafAdjustable: (id) => activate.selectorLeafAdjustTarget(id, deps) !== null
			}
		);
	const select = (id) => {
		session.clear();
		session.run('selector.drill-in', { timeStamp: 0, startId: id });
		assert.equal(session.selectedLeaf(), id);
	};
	return { leaves, inner, deps, press, select };
}

// [if] a leaf element is itself an enabled button and Enter activates it [then] that button is clicked once, nothing throws, [else stop].
test('KEYB-14379 Enter on a leaf that is itself a button clicks that button', () => {
	const h = harness();
	const pad = h.leaves.get('deck.1.pad.1');
	assert.equal(activate.selectorLeafPrimaryControl(pad), pad);
	h.select('deck.1.pad.1');
	assert.equal(h.press('Enter'), true);
	assert.deepEqual(pad.counts, { clicks: 1, focuses: 0 });
});

// [if] a leaf element is itself a role="slider" [then] Enter focuses it and adjust forwards one arrow keydown to it, [else stop].
test('KEYB-14379 a leaf that is itself a slider takes focus on Enter and the forwarded arrow', (t) => {
	const saved = globalThis.KeyboardEvent;
	globalThis.KeyboardEvent = FakeKeyboardEvent;
	t.after(() => {
		if (saved === undefined) delete globalThis.KeyboardEvent;
		else globalThis.KeyboardEvent = saved;
	});
	const h = harness();
	const pitch = h.leaves.get('deck.1.pitch');
	assert.deepEqual(activate.selectorLeafAdjustTarget('deck.1.pitch', h.deps), {
		kind: 'slider',
		control: pitch
	});
	h.select('deck.1.pitch');
	assert.equal(h.press('Enter'), true);
	assert.deepEqual(pitch.counts, { clicks: 0, focuses: 1 });
	assert.equal(h.press('ArrowUp'), true);
	assert.deepEqual(
		pitch.keydowns.map((e) => [e.type, e.key]),
		[['keydown', 'ArrowUp']]
	);
	assert.equal(activate.isForwardedSelectorKey(pitch.keydowns[0]), true);
});

// [if] a leaf element is a disabled button with no control inside [then] activate throws the clear "has no control to activate" error, [else stop].
test('KEYB-14379 a disabled button leaf with no control inside keeps the clear error', () => {
	const h = harness();
	const undo = h.leaves.get('deck.1.undo');
	assert.equal(activate.selectorLeafPrimaryControl(undo), null);
	assert.throws(
		() => activate.createSelectorLeafHost('deck.1.undo', h.deps).activate(),
		/^Error: Selector leaf deck\.1\.undo has no control to activate$/
	);
	assert.equal(undo.counts.clicks, 0);
});

// [if] a leaf wraps a button (the existing shape) [then] Enter clicks the inner button, not the leaf, [else stop].
test('KEYB-14379 a leaf that wraps a button still clicks the inner button', () => {
	const h = harness();
	const transport = h.leaves.get('deck.1.transport');
	h.select('deck.1.transport');
	assert.equal(h.press('Enter'), true);
	assert.deepEqual(h.inner.counts, { clicks: 1, focuses: 0 });
	assert.deepEqual(transport.counts, { clicks: 0, focuses: 0 });
});
