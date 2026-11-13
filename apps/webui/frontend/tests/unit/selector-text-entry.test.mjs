/**
 * Issue #12255: the selector keys (W A S D, E, Q, and Enter or the arrows on a selected leaf)
 * never act while an editable control owns typing, and the gate is the shared text-entry
 * predicate (`src/lib/keyboard/text-entry-target.ts`), not a guard local to the selector.
 *
 * The real router and selector session run over a synthesized /performance layout; only the
 * DOM measurement (the node snapshot) and the focused targets are fixtures.
 *
 * Regression lines:
 * - if W in a focused text input moves the selector or is prevented then broken
 * - if Q in a focused contenteditable field drills the selector out or is prevented then broken
 * - if W from focused, non-editable performance chrome stops moving the selector then broken
 * - if the router and isTextEntryTarget disagree about any target then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const routing = await loadTypeScriptModule('src/lib/rb/performance-shortcut-routing.ts');
const textEntry = await loadTypeScriptModule('src/lib/keyboard/text-entry-target.ts');
const sessions = await loadTypeScriptModule('src/lib/keyboard/selector-session.ts');
const ring = await loadTypeScriptModule('src/lib/keyboard/selector-ring-state.svelte.ts');
const register = await loadTypeScriptModule('src/lib/keyboard/selector-register.ts');

const node = (id, parentId, kind, x, y, width, height) => ({
	id,
	parentId,
	kind,
	enabled: true,
	rect: { x, y, width, height }
});

const LAYOUT = [
	node('decks', null, 'section', 0, 240, 1600, 400),
	node('topbar', null, 'section', 0, 0, 1600, 40),
	node('deck.1', 'decks', 'group', 0, 240, 600, 200),
	node('deck.3', 'decks', 'group', 0, 440, 600, 200),
	node('mixer', 'decks', 'group', 600, 240, 400, 400),
	node('deck.1.header', 'deck.1', 'leaf', 0, 240, 600, 40),
	node('deck.1.cues', 'deck.1', 'leaf', 0, 280, 600, 60)
];

const target = (tagName, { type = null, contentEditable = false, role = null } = {}) => ({
	tagName,
	type,
	isContentEditable: contentEditable,
	getAttribute: (name) => (name === 'role' ? role : null)
});

const TEXT_INPUT = target('INPUT', { type: 'text' });
const CONTENT_EDITABLE = target('DIV', { contentEditable: true });
/** A focusable deck panel: page chrome that edits no text (tabindex, a landmark role). */
const DECK_PANEL = target('DIV', { role: 'region' });

/** Wires router + session the way installPerformanceHotkeys does, over a fixture snapshot. */
function selectorHarness() {
	ring.clearSelectorRingTarget();
	const session = sessions.createSelectorSession({
		snapshot: () => register.resolveSelectorNodes(LAYOUT),
		version: () => 1,
		showRing: ring.setSelectorRingTarget,
		hideRing: ring.clearSelectorRingTarget
	});
	const press = (key, { target: focused = DECK_PANEL, startId = null } = {}) => {
		let prevented = 0;
		const event = {
			code: '',
			key,
			target: focused,
			metaKey: false,
			ctrlKey: false,
			altKey: false,
			shiftKey: false,
			repeat: false,
			timeStamp: 0,
			preventDefault: () => (prevented += 1)
		};
		const handlers = {
			toggleRecentPlay: () => {},
			selectorCommand: (command) => session.run(command, { timeStamp: 0, startId }),
			clearSelector: () => session.clear()
		};
		const handled = routing.handlePerformanceShortcutKeydown(event, handlers, {
			selectorActive: session.selected() !== null,
			selectorInLibrary: false
		});
		return { handled, prevented };
	};
	return { session, press };
}

const IDLE = { handled: false, prevented: 0 };
const TAKEN = { handled: true, prevented: 1 };

// [if] a text input has focus and W is pressed [then] the selector stays put and the key is not prevented, [else stop].
test('A11Y-13794 W in a focused text input types instead of moving the selector', () => {
	const { session, press } = selectorHarness();
	press('s', { startId: 'deck.1' });
	assert.equal(session.selected(), 'deck.1');
	assert.deepEqual(press('w', { target: TEXT_INPUT }), IDLE);
	assert.equal(session.selected(), 'deck.1');
});

// [if] a contenteditable field has focus and Q is pressed [then] the selector does not drill out and the key is not prevented, [else stop].
test('A11Y-13794 Q in a focused contenteditable field types instead of drilling the selector out', () => {
	const { session, press } = selectorHarness();
	press('e', { startId: 'deck.1' });
	press('e');
	assert.equal(session.selected(), 'deck.1.header');
	assert.deepEqual(press('q', { target: CONTENT_EDITABLE }), IDLE);
	assert.equal(session.selected(), 'deck.1.header');
	assert.deepEqual(press('q'), TAKEN);
	assert.equal(session.selected(), 'deck.1');
});

// [if] non-editable performance chrome (a deck panel or a button) has focus and W is pressed [then] the selector moves, [else stop].
test('A11Y-13794 W from focused, non-editable performance chrome still moves the selector', () => {
	for (const chrome of [DECK_PANEL, target('BUTTON'), target('DIV')]) {
		const { session, press } = selectorHarness();
		press('s', { target: chrome, startId: 'deck.3' });
		assert.equal(session.selected(), 'deck.3', chrome.tagName);
		assert.deepEqual(press('w', { target: chrome }), TAKEN, chrome.tagName);
		assert.equal(session.selected(), 'deck.1', chrome.tagName);
	}
});

// [if] any focused target meets a selector key [then] the router yields exactly when isTextEntryTarget says text entry, [else stop].
test('A11Y-13794 the selector keys yield exactly where the shared text-entry predicate says so', () => {
	const targets = [
		TEXT_INPUT,
		CONTENT_EDITABLE,
		target('INPUT', { type: 'search' }),
		target('INPUT', { type: 'number' }),
		target('TEXTAREA'),
		target('SELECT'),
		target('DIV', { role: 'textbox' }),
		target('DIV', { role: 'combobox' }),
		DECK_PANEL,
		target('BUTTON'),
		target('INPUT', { type: 'checkbox' }),
		target('DIV', { role: 'slider' }),
		target('DIV'),
		null
	];
	const context = {
		selectorActive: true,
		selectorInLibrary: false,
		selectorLeafId: 'mixer.ch1.high',
		// #14324: the dial leaf is adjustable, as the hotkeys host reports it from the knob registry.
		selectorLeafAdjustable: (leafId) => leafId === 'mixer.ch1.high'
	};
	const keys = ['w', 'a', 's', 'd', 'e', 'q', 'Enter', 'ArrowUp', 'ArrowDown'];
	for (const focused of targets) {
		const typing = textEntry.isTextEntryTarget(focused);
		for (const key of keys) {
			const event = {
				code: '',
				key,
				target: focused,
				metaKey: false,
				ctrlKey: false,
				altKey: false,
				shiftKey: false,
				repeat: false
			};
			const action = routing.resolvePerformanceShortcutAction(event, context);
			const label = `${focused?.tagName ?? 'null'} ${focused?.getAttribute('role') ?? ''} ${key}`;
			if (typing) assert.equal(action, null, label);
			else assert.match(action?.kind ?? 'none', /^selector(-leaf)?$/, label);
		}
	}
	assert.equal(textEntry.isTextEntryTarget(TEXT_INPUT), true);
	assert.equal(textEntry.isTextEntryTarget(DECK_PANEL), false);
});
