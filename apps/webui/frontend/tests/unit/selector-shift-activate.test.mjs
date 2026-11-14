// requirement: KEYB-14533
/**
 * #14522: Shift+Enter on the selected selector leaf presses its control as a Shift+click.
 *
 * Stem mute buttons solo on Shift+click; before this the selector could only send a plain
 * click. The real router, selector session, command registry and leaf host run together; only
 * the DOM (the node snapshot and the elements) is a fixture, and node's missing MouseEvent is a
 * stand-in that keeps `shiftKey`.
 *
 * Regression lines:
 * - if Shift+Enter on a leaf wrapping a button does not deliver one click with shiftKey true then broken
 * - if bare Enter on that leaf stops being a plain click (shiftKey false) then broken
 * - if Shift+Enter is taken by the selector while a text field has focus then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const routing = await loadTypeScriptModule('src/lib/rb/performance-shortcut-routing.ts');
const sessions = await loadTypeScriptModule('src/lib/keyboard/selector-session.ts');
const registry = await loadTypeScriptModule('src/lib/keyboard/command-registry.ts');
const activate = await loadTypeScriptModule('src/lib/keyboard/selector-activate.ts');
const register = await loadTypeScriptModule('src/lib/keyboard/selector-register.ts');

const LAYOUT = [
	{
		id: 'mixer',
		parentId: null,
		kind: 'section',
		enabled: true,
		rect: { x: 0, y: 0, width: 200, height: 100 }
	},
	{
		id: 'mixer.ch1.stem.vocal',
		parentId: 'mixer',
		kind: 'leaf',
		enabled: true,
		rect: { x: 0, y: 0, width: 100, height: 100 }
	}
];

const PAGE = { tagName: 'DIV', getAttribute: () => null };
const TEXT_FIELD = { tagName: 'INPUT', type: 'text', getAttribute: () => null };

/** node has Event but no MouseEvent; this keeps the modifier the leaf host sets. */
class FakeMouseEvent extends Event {
	constructor(type, init) {
		super(type, init);
		this.shiftKey = init.shiftKey === true;
	}
}

/** A stem mute button: click() is the browser's plain click, dispatchEvent takes a built one. */
function muteButton() {
	const clicks = [];
	return {
		tagName: 'BUTTON',
		clicks,
		getAttribute: () => null,
		matches: () => false,
		click: () => clicks.push({ type: 'click', shiftKey: false }),
		focus: () => {
			throw new Error('a button is pressed, never focused');
		},
		dispatchEvent: (event) => {
			clicks.push({ type: event.type, shiftKey: event.shiftKey });
			return true;
		}
	};
}

/** Wires router, session, registry and leaf host the way installPerformanceHotkeys does. */
function harness(t) {
	const saved = globalThis.MouseEvent;
	globalThis.MouseEvent = FakeMouseEvent;
	t.after(() => {
		if (saved === undefined) delete globalThis.MouseEvent;
		else globalThis.MouseEvent = saved;
	});
	const button = muteButton();
	const leaf = {
		tagName: 'DIV',
		getAttribute: (name) => (name === 'data-sel-node' ? 'mixer.ch1.stem.vocal' : null),
		matches: () => false,
		querySelector: () => button
	};
	const deps = {
		findLeaf: (id) => (id === 'mixer.ch1.stem.vocal' ? leaf : null),
		readControl: () => null,
		nudgeControl: () => {
			throw new Error('no knob in this layout');
		},
		nudgeTempo: () => {
			throw new Error('no jog in this layout');
		},
		keyStep: 0.05
	};
	const session = sessions.createSelectorSession({
		snapshot: () => register.resolveSelectorNodes(LAYOUT),
		version: () => 1,
		showRing: () => {},
		hideRing: () => {}
	});
	session.run('selector.drill-in', { timeStamp: 0, startId: 'mixer.ch1.stem.vocal' });
	assert.equal(session.selectedLeaf(), 'mixer.ch1.stem.vocal');
	const handlers = {
		selectorCommand: (command) => session.run(command, { timeStamp: 0, startId: null }),
		clearSelector: () => session.clear(),
		selectorLeafCommand: (command) =>
			registry.runCommand(command, {
				timeStamp: 0,
				selectorLeaf: activate.createSelectorLeafHost(session.selectedLeaf(), deps)
			})
	};
	const press = (key, { shiftKey, target }) =>
		routing.handlePerformanceShortcutKeydown(
			{
				code: key,
				key,
				target,
				metaKey: false,
				ctrlKey: false,
				altKey: false,
				shiftKey,
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
	return { button, press };
}

// [if] a leaf wrapping a button is selected and Shift+Enter is pressed [then] the button gets one click with shiftKey true, [else stop].
test('KEYB-14522 Shift+Enter on a leaf wrapping a button shift-clicks it once', (t) => {
	const h = harness(t);
	assert.equal(h.press('Enter', { shiftKey: true, target: PAGE }), true);
	assert.deepEqual(h.button.clicks, [{ type: 'click', shiftKey: true }]);
	const byId = new Map(registry.listPerformanceCommandMetadata().map((c) => [c.id, c.defaultKeys]));
	assert.deepEqual(byId.get('selector.shift-activate'), ['Shift+Enter']);
});

// [if] bare Enter is pressed on the same leaf [then] the button gets one click with shiftKey false, as before, [else stop].
test('KEYB-14522 bare Enter on the same leaf stays a plain click', (t) => {
	const h = harness(t);
	assert.equal(h.press('Enter', { shiftKey: false, target: PAGE }), true);
	assert.deepEqual(h.button.clicks, [{ type: 'click', shiftKey: false }]);
});

// [if] Shift+Enter is pressed while a text field has focus [then] the selector leaves the key alone and nothing is clicked, [else stop].
test('KEYB-14522 Shift+Enter in a text field is not taken by the selector', (t) => {
	const h = harness(t);
	assert.equal(h.press('Enter', { shiftKey: true, target: TEXT_FIELD }), false);
	assert.deepEqual(h.button.clicks, []);
	// Positive control: the same press from the page is taken.
	assert.equal(h.press('Enter', { shiftKey: true, target: PAGE }), true);
	assert.equal(h.button.clicks.length, 1);
});
