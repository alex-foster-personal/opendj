/**
 * @pytest.mark.requirement A11Y-01
 * Issue #12279: the Cmd-K palette and the /performance global shortcuts, through
 * the REAL installed listener (installPerformanceHotkeys). The fake palette input
 * carries the attributes CommandBarV2.svelte's query input renders (an INPUT of
 * type text, role combobox); "closed" means focus is back on page chrome. Only
 * the IPC send is replaced (fixtures/performance-hotkeys-command-palette-stub-ipc.ts).
 * The browser half lives in tests/e2e/performance-hotkeys-command-palette.spec.ts.
 *
 * Regression lines:
 * - if a bare performance letter in the palette query is default-prevented then the DJ cannot type a search
 * - if Space in the palette query sends a transport command then searching starts or stops a deck
 * - if a shortcut stays dead on page chrome after the palette closes then the keyboard stops driving decks
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const STUB_IPC = `${FRONTEND_ROOT}/tests/unit/fixtures/performance-hotkeys-command-palette-stub-ipc.ts`;

/** C cue, V beat sync, K master tempo, P pitch range, M comment pin. */
const PERFORMANCE_LETTERS = ['c', 'v', 'k', 'p', 'm'];

class FakeElement {}
class FakeHTMLElement extends FakeElement {}

const saved = {};
const listeners = new Map();
let m;
let uninstall;

/** A stand-in element carrying only the tag, type and attributes the listener reads. */
function fakeElement(tagName, attrs = {}, type = '') {
	const el = new FakeHTMLElement();
	el.tagName = tagName;
	el.isContentEditable = false;
	el.type = type;
	el.getAttribute = (name) => attrs[name] ?? null;
	el.hasAttribute = (name) => name in attrs;
	el.closest = () => null;
	return el;
}

/** The Cmd-K query input as CommandBarV2.svelte renders it. */
function paletteInput() {
	return fakeElement(
		'INPUT',
		{
			class: 'q',
			role: 'combobox',
			'aria-expanded': 'true',
			'aria-controls': 'cmdk2-list',
			autocomplete: 'off',
			spellcheck: 'false',
			'data-testid': 'command-bar-input'
		},
		'text'
	);
}

/** Page chrome once the palette has closed and focus left its input. */
function pageChrome() {
	return fakeElement('BODY');
}

/** Dispatches one plain keydown at `target` through the installed capture listener. */
function press(key, target) {
	const code = key === ' ' ? 'Space' : `Key${key.toUpperCase()}`;
	const event = {
		type: 'keydown',
		code,
		key,
		target,
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		shiftKey: false,
		repeat: false,
		timeStamp: 1000,
		defaultPrevented: false,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopPropagation() {}
	};
	const onKey = listeners.get('keydown');
	assert.ok(onKey !== undefined, 'installPerformanceHotkeys must register a keydown listener');
	onKey.fn(event);
	return event;
}

function transportSends() {
	return m.sent.filter((c) => c.type === 'load_play_intent' || c.type === 'play');
}

before(async () => {
	for (const key of ['window', 'Element', 'HTMLElement']) saved[key] = globalThis[key];
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = FakeHTMLElement;
	// Module-load listeners are not under test; keep only what the install registers.
	let recording = false;
	globalThis.window = {
		addEventListener: (type, fn, opts) => {
			if (recording) listeners.set(type, { fn, capture: opts?.capture === true });
		},
		removeEventListener: (type) => listeners.delete(type),
		// prefs.svelte reads saved settings at import; an empty store is a first boot.
		localStorage: {
			getItem: () => null,
			setItem: () => {},
			removeItem: () => {}
		}
	};
	m = await loadTypeScriptModule(
		'tests/unit/fixtures/performance-hotkeys-command-palette-entry.ts',
		{
			alias: { '$lib/rb/performance-ipc.svelte': STUB_IPC }
		}
	);
	recording = true;
	uninstall = m.installPerformanceHotkeys();
	recording = false;
	assert.equal(listeners.get('keydown')?.capture, true, 'keydown must be a capture listener');
});

after(() => {
	uninstall?.();
	for (const [key, value] of Object.entries(saved)) {
		if (value === undefined) delete globalThis[key];
		else globalThis[key] = value;
	}
});

beforeEach(() => {
	// A pending load-play is a transport target Space would toggle; a live
	// feedback daemon is what lets M arm a comment pin.
	m.beginPendingLoadPlay(1, false);
	m.sent.length = 0;
	m.feedbackState.availability = 'ok';
	m.feedbackState.placementArmed = false;
});

test('[if] the palette input has focus [then] bare performance letters reach the query unprevented and send nothing, [else stop].', async () => {
	const input = paletteInput();
	for (const key of PERFORMANCE_LETTERS) {
		const event = press(key, input);
		assert.equal(event.defaultPrevented, false, `${key} must type into the palette query`);
	}
	await Promise.resolve();
	assert.deepEqual(m.sent, []);
	assert.equal(m.feedbackState.placementArmed, false, 'm in the query must not arm a comment pin');
});

test('[if] the palette input has focus [then] Space types a space and toggles no deck, [else stop].', async () => {
	const event = press(' ', paletteInput());
	await Promise.resolve();
	assert.equal(event.defaultPrevented, false, 'Space must type into the palette query');
	assert.deepEqual(transportSends(), []);
});

test('[if] the palette closes and page chrome has focus [then] Space toggles the deck and the letters are shortcuts again, [else stop].', async () => {
	// The same keys at the open palette first, so the resume is a real transition.
	const input = paletteInput();
	assert.equal(press(' ', input).defaultPrevented, false);
	assert.equal(press('m', input).defaultPrevented, false);

	const chrome = pageChrome();
	const space = press(' ', chrome);
	await Promise.resolve();
	assert.equal(space.defaultPrevented, true, 'an unprevented Space scrolls the page');
	assert.equal(transportSends().length, 1);
	assert.equal(transportSends()[0].type, 'load_play_intent');
	assert.equal(transportSends()[0].desired_play, true);

	for (const key of PERFORMANCE_LETTERS) {
		assert.equal(press(key, chrome).defaultPrevented, true, `${key} must be a shortcut again`);
	}
	assert.equal(m.feedbackState.placementArmed, true, 'm on page chrome arms a comment pin');
});
