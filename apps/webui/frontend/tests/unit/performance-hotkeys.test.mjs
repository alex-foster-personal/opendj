/**
 * /performance keyboard shortcuts (issue #3528).
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('LATENCY-02 Cmd+Space sends quantize when starting a paused deck', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /quantize === true && playing \? \{ quantize: true \}/);
});

test('the global performance listener routes through the shared text-entry predicate', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /handlePerformanceShortcutKeydown\(/);
	assert.match(source, /armPinPlacement/);
	assert.doesNotMatch(source, /isNativeInteractiveTarget/);
});

test('settings hotkeys has no bare single-key branch to gate on the text-entry predicate', async () => {
	// review r3549 P2: this file used to gate its whole listener on
	// isTextEntryTarget, which also blocked the Cmd+, chord and Escape from
	// firing while a settings text field had focus - neither branch here is
	// a bare single key that could edit typed text, so the fix removes the
	// guard entirely rather than narrowing it, unlike the shortcut modules
	// that do keep it for their bare-letter/Space branches.
	const source = await readFile('src/lib/settings/hotkeys.ts', 'utf8');
	assert.doesNotMatch(source, /import\s*\{[^}]*isTextEntryTarget/);
	assert.doesNotMatch(source, /isTextEntryTarget\(/);
});

// ----- KEYB-13755 (#9395): W A S D / E / Q drive the keyboard selector, Esc clears it -----
// The real router and selector session run against a synthesized /performance layout; only
// the DOM measurement (the node snapshot) is a fixture. Rects are viewport pixels.

const routing = await loadTypeScriptModule('src/lib/rb/performance-shortcut-routing.ts');
const sessions = await loadTypeScriptModule('src/lib/keyboard/selector-session.ts');
const ring = await loadTypeScriptModule('src/lib/keyboard/selector-ring-state.svelte.ts');
const register = await loadTypeScriptModule('src/lib/keyboard/selector-register.ts');
const browserNav = await loadTypeScriptModule('src/lib/rb/browser-navigation.ts');

const node = (id, parentId, kind, x, y, width, height) => ({
	id,
	parentId,
	kind,
	enabled: true,
	rect: { x, y, width, height }
});

const LAYOUT = [
	node('library', null, 'section', 0, 640, 1600, 400),
	node('decks', null, 'section', 0, 240, 1600, 400),
	node('topbar', null, 'section', 0, 0, 1600, 40),
	node('waveforms', null, 'section', 0, 40, 1600, 200),
	node('deck.1', 'decks', 'group', 0, 240, 600, 200),
	node('deck.3', 'decks', 'group', 0, 440, 600, 200),
	node('mixer', 'decks', 'group', 600, 240, 400, 400),
	node('deck.2', 'decks', 'group', 1000, 240, 600, 200),
	node('deck.1.cues', 'deck.1', 'leaf', 0, 280, 600, 60),
	node('deck.1.header', 'deck.1', 'leaf', 0, 240, 600, 40),
	node('deck.1.transport', 'deck.1', 'leaf', 0, 340, 200, 100)
];

const PAGE = { tagName: 'DIV', getAttribute: () => null };
const TEXT = { tagName: 'INPUT', type: 'text', getAttribute: () => null };

/** Wires router + session the way installPerformanceHotkeys does, over a fixture snapshot. */
function selectorHarness(records = LAYOUT) {
	const state = { version: 1, snapshots: 0, plays: 0, records };
	ring.clearSelectorRingTarget();
	const session = sessions.createSelectorSession({
		snapshot: () => {
			state.snapshots += 1;
			return register.resolveSelectorNodes(state.records);
		},
		version: () => state.version,
		showRing: ring.setSelectorRingTarget,
		hideRing: ring.clearSelectorRingTarget
	});
	const press = (key, { target = PAGE, startId = null, library = false, settingsOpen } = {}) => {
		let prevented = 0;
		const event = {
			code: key === ' ' ? 'Space' : '',
			key,
			target,
			metaKey: false,
			ctrlKey: false,
			altKey: false,
			shiftKey: false,
			repeat: false,
			timeStamp: 0,
			preventDefault: () => (prevented += 1)
		};
		const handlers = {
			toggleRecentPlay: () => (state.plays += 1),
			selectorCommand: (command) => session.run(command, { timeStamp: 0, startId }),
			clearSelector: () => session.clear()
		};
		const handled = routing.handlePerformanceShortcutKeydown(event, handlers, {
			settingsOpen,
			libraryFocus: library,
			selectorActive: session.selected() !== null,
			selectorInLibrary: session.selectionWithin('library')
		});
		return { handled, prevented };
	};
	return { state, session, press };
}

// [if] d is pressed outside text entry with the selector on the left deck column [then] the selection moves to the mixer, [else stop].
test('KEYB-13755 D moves the selector from the left deck column to the mixer', () => {
	const { session, press } = selectorHarness();
	assert.deepEqual(press('d', { startId: 'deck.1' }), { handled: true, prevented: 1 });
	assert.equal(session.selected(), 'deck.1');
	assert.deepEqual(press('d'), { handled: true, prevented: 1 });
	assert.equal(session.selected(), 'mixer');
	assert.equal(ring.selectorRing.target.id, 'mixer');
	assert.deepEqual(ring.selectorRing.target.rect, { x: 600, y: 240, width: 400, height: 400 });
});

// [if] w is pressed while a text input is focused [then] the selector does not move and the key is not prevented, [else stop].
test('KEYB-13755 W in a text input types instead of moving the selector', () => {
	const { session, press } = selectorHarness();
	press('s', { startId: 'deck.1' });
	assert.deepEqual(press('w', { target: TEXT }), { handled: false, prevented: 0 });
	assert.equal(session.selected(), 'deck.1');
	assert.deepEqual(press('w', { settingsOpen: true }), { handled: false, prevented: 0 });
	assert.equal(session.selected(), 'deck.1');
});

// [if] the selection is in the library and s is pressed [then] the library row key goes through and the selector stays, [else stop].
test('KEYB-13755 S inside the library section moves library rows, A/D still move the selector', () => {
	const { session, press } = selectorHarness();
	press('s', { startId: 'decks' });
	assert.deepEqual(press('s'), { handled: true, prevented: 1 });
	assert.equal(session.selected(), 'library');
	assert.deepEqual(press('s'), { handled: false, prevented: 0 });
	assert.deepEqual(press('w'), { handled: false, prevented: 0 });
	assert.equal(browserNav.browserSelectionDelta('s'), 1);
	assert.equal(session.selected(), 'library');
	assert.deepEqual(press('d'), { handled: true, prevented: 1 });
	assert.equal(session.selected(), 'library');
});

// [if] no selection exists and a W A S D key comes from inside the library [then] the library keeps it, [else stop].
test('KEYB-13755 with no selection the library keeps W A S D from its own rows', () => {
	const { session, press } = selectorHarness();
	for (const key of ['w', 'a', 's', 'd'])
		assert.deepEqual(press(key, { library: true }), { handled: false, prevented: 0 }, key);
	assert.equal(session.selected(), null);
	assert.deepEqual(press('e', { library: true, startId: 'library' }), {
		handled: true,
		prevented: 1
	});
	assert.equal(session.selected(), 'library');
});

// [if] e then e is pressed from the decks section [then] the selection is deck 1, then deck 1's header node, [else stop].
test('KEYB-13755 E then E drills from the decks section to deck 1, then its header', () => {
	const { session, press } = selectorHarness();
	press('e', { startId: 'decks' });
	assert.equal(session.selected(), 'decks');
	press('e');
	assert.equal(session.selected(), 'deck.1');
	press('e');
	assert.equal(session.selected(), 'deck.1.header');
	press('q');
	assert.equal(session.selected(), 'deck.1');
});

// [if] Esc is pressed with a live selection [then] the selector and ring clear and Space still toggles play, [else stop].
test('KEYB-13755 Esc clears the selector without taking the key; Space still toggles play', () => {
	const { state, session, press } = selectorHarness();
	press('d', { startId: 'deck.1' });
	assert.notEqual(ring.selectorRing.target, null);
	assert.deepEqual(press('Escape'), { handled: false, prevented: 0 });
	assert.equal(session.selected(), null);
	assert.equal(ring.selectorRing.target, null);
	assert.deepEqual(press('Escape'), { handled: false, prevented: 0 });
	// The Space branch asks `target instanceof Element`; node has no DOM, so give it the class.
	const nodeElement = globalThis.Element;
	globalThis.Element = class Element {};
	try {
		assert.deepEqual(press(' '), { handled: true, prevented: 1 });
	} finally {
		globalThis.Element = nodeElement;
	}
	assert.equal(state.plays, 1);
});

// [if] the first selector key arrives [then] the tree is built once, and rebuilt only after the registry version moves, [else stop].
test('KEYB-13755 the tree is snapshotted lazily and rebuilt when the node registry changes', () => {
	const { state, session, press } = selectorHarness();
	assert.equal(state.snapshots, 0);
	press('a', { startId: 'deck.2' });
	press('a');
	assert.equal(session.selected(), 'mixer');
	assert.equal(state.snapshots, 1);
	state.records = LAYOUT.filter((r) => r.id !== 'deck.1' && !r.id.startsWith('deck.1.'));
	state.version += 1;
	press('a');
	assert.equal(state.snapshots, 2);
	assert.equal(session.selected(), 'deck.3');
});

const SECTION_PAGE = { tagName: 'DIV', getAttribute: () => null };
const sectionPress = (key, extra = {}) => ({
	code: '',
	key,
	target: SECTION_PAGE,
	metaKey: false,
	ctrlKey: false,
	altKey: false,
	shiftKey: false,
	...extra
});

test('issue #10263: Shift+] and Shift+[ route to a section jump; the plain brackets still zoom', async () => {
	const routing = await loadTypeScriptModule('src/lib/rb/performance-shortcut-routing.ts');
	const resolve = (e) => routing.resolvePerformanceShortcutAction(e, {});
	assert.deepEqual(resolve(sectionPress('}', { shiftKey: true })), {
		kind: 'section-jump',
		direction: 'next'
	});
	assert.deepEqual(resolve(sectionPress('{', { shiftKey: true })), {
		kind: 'section-jump',
		direction: 'previous'
	});
	// A layout whose shifted bracket is not a brace still routes on the key code.
	assert.deepEqual(resolve(sectionPress(']', { shiftKey: true, code: 'BracketRight' })), {
		kind: 'section-jump',
		direction: 'next'
	});
	assert.deepEqual(resolve(sectionPress(']')), { kind: 'wave-zoom', direction: 'in' });
	assert.deepEqual(resolve(sectionPress('[')), { kind: 'wave-zoom', direction: 'out' });
	const calls = [];
	routing.handlePerformanceShortcutKeydown(
		{ ...sectionPress('}', { shiftKey: true }), preventDefault() {}, timeStamp: 0 },
		{
			toggleRecentPlay() {},
			resizeLast() {},
			exitLast() {},
			armPinPlacement() {},
			waveZoomStep() {},
			sectionJump: (direction) => calls.push(direction)
		}
	);
	assert.deepEqual(calls, ['next']);
});

test('issue #10263: the hotkey handler dispatches section_jump on the transport deck', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(
		source,
		/runPerformanceCommandFromUi\(\{ type: 'section_jump', deck, direction \}\)/
	);
	assert.match(source, /sectionJump: \(direction\) => \{/);
	const registry = await readFile('src/lib/components/rb/hotkeys/hotkeys-registry.ts', 'utf8');
	assert.match(registry, /chord: 'Shift\+\]'/);
	assert.match(registry, /chord: 'Shift\+\['/);
});
